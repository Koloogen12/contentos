"""Thin wrapper around CometAPI (OpenAI-compatible).

We isolate provider behind a single interface so we can later add a fallback
to Anthropic direct without touching call sites.
"""
import json
import logging
import contextlib
import contextvars
from collections.abc import Iterator
from typing import Any

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings

logger = logging.getLogger(__name__)


def _client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.COMETAPI_KEY,
        base_url=settings.COMETAPI_BASE_URL,
    )


# Models that hard-reject the `temperature` param:
#   {"error": {"message": "`temperature` is deprecated for this model."}}
# Every skill call site still passes a temperature (0.7-0.9), so we strip it
# here rather than touching 15 call sites — sampling defaults to the model's
# own, which is what those models want anyway.
# Модели, отвергающие temperature с 400 вместо того чтобы её проигнорировать.
# Без записи здесь каждый запрос делал бы лишний круг: первый вызов падает,
# перехватчик снимает параметр и повторяет. На Sonnet 5 и Opus 5 отвергаются
# все сэмплирующие параметры — стиль задаётся промптом, а не temperature.

_anthropic_client: AsyncAnthropic | None = None


def _anthropic() -> AsyncAnthropic:
    """Клиент Anthropic для генерации текста.

    Прокси остался только там, где у Anthropic нет аналога: эмбеддинги,
    распознавание речи, генерация картинок. Разделение проходит по
    возможностям провайдера, а не по типу задачи.
    """
    global _anthropic_client
    if _anthropic_client is None:
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError(
                "Генерация текста не настроена: на сервере нет ANTHROPIC_API_KEY."
            )
        _anthropic_client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _anthropic_client


# Системный промпт уходит блоком с cache_control: бренд, голос и редполитика
# в нём не меняются между запусками, а весят несколько тысяч токенов.
# Чтение из кэша стоит 10% от обычного входа, запись — 125%, так что второй
# и последующие запросы в пределах пяти минут окупают первый с запасом.
#
# Меньше порога кэш молча не создаётся (у Sonnet 5 это 1024 токена), поэтому
# короткие системные промпты отправляем без пометки — она бы только мешала
# читать код, обещая экономию, которой нет.
_CACHE_MIN_CHARS = 3000


def _system_blocks(system: str) -> list[dict[str, Any]]:
    block: dict[str, Any] = {"type": "text", "text": system}
    if len(system) >= _CACHE_MIN_CHARS:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


MODELS_WITHOUT_TEMPERATURE: frozenset[str] = frozenset(
    {"claude-opus-4-8", "claude-opus-5", "claude-sonnet-5", "claude-fable-5"}
)


def _supports_temperature(model: str) -> bool:
    return model not in MODELS_WITHOUT_TEMPERATURE


# ---------------------------------------------------------------------------
# Учёт расхода токенов
# ---------------------------------------------------------------------------
#
# Один запуск скилла может сделать несколько вызовов модели (повтор при
# отказе прокси, второй проход у tweak). Считать нужно все, поэтому счётчик
# живёт в contextvar: воркер открывает его на время запуска, а клиент
# складывает туда каждый ответ, не зная, кто его позвал.
#
# contextvar, а не глобальная переменная: воркер обрабатывает задачи
# конкурентно, и общий счётчик смешал бы расход разных запусков.

_usage_sink: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "ai_usage_sink", default=None
)


@contextlib.contextmanager
def collect_usage() -> Iterator[dict[str, Any]]:
    """Собрать расход всех вызовов модели внутри блока."""
    acc: dict[str, Any] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "model": None,
        "calls": 0,
    }
    token = _usage_sink.set(acc)
    try:
        yield acc
    finally:
        _usage_sink.reset(token)


def _record_usage(response: Any, model: str) -> None:
    """Сложить расход одного ответа в счётчик текущего запуска.

    Поля различаются по провайдеру: у Anthropic это input_tokens /
    output_tokens и отдельно записанное и прочитанное из кэша, у
    OpenAI-совместимого прокси — prompt_tokens / completion_tokens.
    Разбираем оба, потому что эмбеддинги и распознавание речи остались там.
    """
    acc = _usage_sink.get()
    if acc is None:
        return
    usage = getattr(response, "usage", None)
    if usage is None:
        return

    cache_read = getattr(usage, "cache_read_input_tokens", None)
    if cache_read is not None:
        # Anthropic: во input_tokens кэш не входит, поэтому складываем сами —
        # иначе стоимость запуска будет занижена на размер префикса.
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        acc["input_tokens"] += (
            (getattr(usage, "input_tokens", 0) or 0) + cache_write + cache_read
        )
        acc["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
        acc["cached_input_tokens"] += cache_read
    else:
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", None) if details else None
        acc["input_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
        acc["output_tokens"] += getattr(usage, "completion_tokens", 0) or 0
        acc["cached_input_tokens"] += cached or 0

    acc["model"] = model
    acc["calls"] += 1


async def _direct_completion(
    *,
    system: str,
    user: str,
    model: str | None = None,
    json_mode: bool = False,
    temperature: float = 0.7,
    max_tokens: int = 3000,
) -> str:
    """Одиночный запрос. Возвращает текст ответа.

    Раньше здесь была защита от особенностей прокси: он подмешивал
    собственные инструменты, и модель вместо текста возвращала заготовки
    вызовов. Напрямую этой проблемы нет — без объявленных инструментов
    вызывать нечего, поэтому защита убрана вместе с прокси.

    `temperature` и `json_mode` в сигнатуре остались, чтобы не править
    четырнадцать мест вызова, но обе игнорируются: Sonnet 5 отвергает
    сэмплирующие параметры, а формат ответа задаётся промптом — у каждого
    скилла он уже требует JSON, и разбор с восстановлением ниже рассчитан
    именно на это.
    """
    resolved_model = model or settings.ANTHROPIC_MODEL
    return await _anthropic_message(
        model=resolved_model,
        system=system,
        messages=[{"role": "user", "content": user}],
        max_tokens=max_tokens,
    )


async def _direct_conversation(
    *,
    system: str,
    history: list[dict[str, str]],
    model: str | None = None,
    max_tokens: int = 4000,
    temperature: float = 0.7,
) -> str:
    """Многоходовой диалог. `history` — список {role, content} от старых к
    новым. Используется LLM-нодой, где пользователь продолжает беседу.
    """
    messages: list[dict[str, str]] = []
    for m in history:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    if not messages:
        raise ValueError("Пустая история диалога")
    # Первым обязан идти пользователь: у Anthropic история, начинающаяся с
    # ответа ассистента, отклоняется.
    if messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": "(начало диалога)"})

    return await _anthropic_message(
        model=model or settings.ANTHROPIC_MODEL,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
    )


# Выше этого порога ответ забираем потоком: длинный ответ на обычном
# запросе упирается в таймаут HTTP, и SDK такой вызов отклоняет заранее.
_STREAM_ABOVE_TOKENS = 16_000


async def _anthropic_message(
    *,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
) -> str:
    """Один вызов модели. Общая точка для одиночных запросов и диалога."""
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": _system_blocks(system),
        "messages": messages,
    }
    client = _anthropic()

    if max_tokens > _STREAM_ABOVE_TOKENS:
        async with client.messages.stream(**kwargs) as stream:
            response = await stream.get_final_message()
    else:
        response = await client.messages.create(**kwargs)

    _record_usage(response, model)

    # Обрыв по лимиту — это не пустой ответ, а обрезанный: у длинных форматов
    # он приходил как «невалидный JSON», и причина была не видна ни из текста
    # ошибки, ни из логов.
    if response.stop_reason == "max_tokens":
        logger.warning(
            "модель %s упёрлась в max_tokens=%s — ответ обрезан", model, max_tokens
        )

    parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    text = "".join(parts).strip()
    if not text:
        if response.stop_reason == "refusal":
            raise RuntimeError("Модель отказалась отвечать на этот материал")
        raise RuntimeError("Модель вернула пустой ответ")
    return text


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def _proxy_completion(
    *,
    system: str,
    user: str,
    model: str | None = None,
    json_mode: bool = False,
    temperature: float = 0.7,
    max_tokens: int = 3000,
) -> str:
    """Single-turn chat. Returns the assistant content string.

    CometAPI's Claude proxy injects workspace tools (file_read, find_by_name,
    etc.) and Claude routinely decides to invoke them instead of writing
    the requested text — surfacing back to us as `[tool_call 0]` content
    or pseudo-XML <tool_calls> blocks. We hard-disable tool use with
    `tool_choice="none"` so the model has to answer in text.
    """
    resolved_model = model or settings.COMETAPI_MODEL
    kwargs: dict[str, Any] = {
        "model": resolved_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        # Strong-arm the proxy: never let the model emit a tool_call. If
        # the underlying transport doesn't recognize this OpenAI-compat
        # field it ignores it (no error), so it's safe as a baseline.
        "tool_choice": "none",
    }
    # Some models (opus-4-8) 400 on `temperature` instead of ignoring it.
    if _supports_temperature(resolved_model):
        kwargs["temperature"] = temperature
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        response = await _client().chat.completions.create(**kwargs)
    except Exception as exc:
        # Some providers reject `tool_choice` when no `tools` are declared,
        # or `temperature` for newer models. Retry once without whichever
        # param the error names. This is the safety net for models not yet
        # in MODELS_WITHOUT_TEMPERATURE — the set above avoids the extra
        # round-trip for the ones we already know about.
        msg = str(exc)
        retriable = False
        if "tool_choice" in msg or "tools" in msg:
            kwargs.pop("tool_choice", None)
            retriable = True
        if "temperature" in msg:
            kwargs.pop("temperature", None)
            retriable = True
            logger.warning(
                "model %s rejected temperature — add it to "
                "MODELS_WITHOUT_TEMPERATURE to skip this retry",
                resolved_model,
            )
        if not retriable:
            raise
        response = await _client().chat.completions.create(**kwargs)

    _record_usage(response, resolved_model)
    content = response.choices[0].message.content
    if content is None:
        raise RuntimeError("Empty completion content from provider")

    # Defensive: if the proxy still surfaces a tool_call stub as content
    # (e.g. literal "[tool_call 0]" or "<tool_calls>..."), retry once
    # WITHOUT json_mode + with an even firmer system prefix forbidding
    # tool use. Helps in the cases CometAPI ignores tool_choice=none.
    stripped = content.strip()
    if (
        stripped.startswith("[tool_call")
        or stripped.startswith("<tool_call")
        or stripped.startswith("<tool_use")
    ):
        kwargs.pop("response_format", None)
        kwargs["messages"][0]["content"] = (
            "DO NOT call any tools. Respond with plain text only. "
            "If a JSON object is requested, output the raw JSON as text.\n\n"
            + system
        )
        response = await _client().chat.completions.create(**kwargs)
        _record_usage(response, resolved_model)
        content = response.choices[0].message.content
        if content is None:
            raise RuntimeError("Empty completion content from provider")
    return content


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def _proxy_conversation(
    *,
    system: str,
    history: list[dict[str, str]],
    model: str | None = None,
    max_tokens: int = 4000,
    temperature: float = 0.7,
) -> str:
    """Multi-turn chat. `history` is a list of {role, content} where role is
    'user' or 'assistant', oldest-first. Returns the assistant's reply text.

    Used by the LLM node, which keeps a running conversation the user can
    continue. Same temperature-stripping + tool_choice safety as
    `chat_completion`, but the message list is the full turn history rather
    than a single system+user pair.
    """
    resolved_model = model or settings.COMETAPI_MODEL
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for m in history:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    if len(messages) == 1:
        raise ValueError("Пустая история диалога")

    kwargs: dict[str, Any] = {
        "model": resolved_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "tool_choice": "none",
    }
    if _supports_temperature(resolved_model):
        kwargs["temperature"] = temperature

    try:
        response = await _client().chat.completions.create(**kwargs)
    except Exception as exc:
        msg = str(exc)
        retriable = False
        if "tool_choice" in msg or "tools" in msg:
            kwargs.pop("tool_choice", None)
            retriable = True
        if "temperature" in msg:
            kwargs.pop("temperature", None)
            retriable = True
        if not retriable:
            raise
        response = await _client().chat.completions.create(**kwargs)

    content = response.choices[0].message.content
    if content is None:
        raise RuntimeError("Empty completion content from provider")
    return content


# ---------------------------------------------------------------------------
# Выбор провайдера
# ---------------------------------------------------------------------------
#
# Anthropic напрямую даёт кэширование промпта — на нашем объёме постоянного
# контекста это главный рычаг экономии. Но их API не принимает запросы с
# российских адресов: сервер в Москве получает 403 «Request not allowed» ещё
# до проверки ключа. Прокси в проекте стоял именно поэтому, а не ради цены.
#
# Поэтому провайдер выбирается настройкой, а не наличием ключа: код для
# прямого доступа готов и ждёт релея в разрешённой стране. Переключение —
# одна переменная окружения, без правок кода.


def _use_direct() -> bool:
    return settings.AI_PROVIDER == "anthropic" and bool(settings.ANTHROPIC_API_KEY)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def chat_completion(
    *,
    system: str,
    user: str,
    model: str | None = None,
    json_mode: bool = False,
    temperature: float = 0.7,
    max_tokens: int = 3000,
) -> str:
    if _use_direct():
        return await _direct_completion(
            system=system, user=user, model=model, json_mode=json_mode,
            temperature=temperature, max_tokens=max_tokens,
        )
    return await _proxy_completion(
        system=system, user=user, model=model, json_mode=json_mode,
        temperature=temperature, max_tokens=max_tokens,
    )


async def chat_conversation(
    *,
    system: str,
    history: list[dict[str, str]],
    model: str | None = None,
    max_tokens: int = 4000,
    temperature: float = 0.7,
) -> str:
    if _use_direct():
        return await _direct_conversation(
            system=system, history=history, model=model,
            max_tokens=max_tokens, temperature=temperature,
        )
    return await _proxy_conversation(
        system=system, history=history, model=model,
        max_tokens=max_tokens, temperature=temperature,
    )


def _extract_json_payload(raw: str) -> str:
    """Strip markdown fence wrappers from a model response.

    CometAPI-proxied Claude often wraps JSON in ``` ```json ... ``` ``` even
    when asked not to. Returns the inner text trimmed.
    """
    s = raw.strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1 :]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        s = s.strip()
    return s


def _balanced_json_slice(s: str) -> str | None:
    """Find the first balanced top-level JSON object/array in `s`.

    Walks the string tracking brace/bracket depth and string state so that
    `{` or `}` inside string literals are not counted. Returns the slice
    `s[start : end+1]` containing exactly one balanced `{...}` or `[...]`,
    or `None` if no balanced top-level structure is found.

    More robust than `s[s.find("{") : s.rfind("}") + 1]` because the latter
    fails when a prose preamble accidentally contains stray braces, or
    when the AI returns multiple JSON blocks (we want only the first).
    """
    i = 0
    n = len(s)
    while i < n and s[i] not in "{[":
        i += 1
    if i == n:
        return None
    opener = s[i]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    j = i
    while j < n:
        c = s[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    return s[i : j + 1]
        j += 1
    return None


def _recover_truncated_object(s: str) -> dict | None:
    """Last-resort recovery for `{"key": [...]}` truncated mid-array.

    Returns the parsed object with the trailing partial array element
    dropped. Handy when the model ran out of tokens halfway through a
    long list. Returns None if nothing salvageable.
    """
    # Find the first `{` and inside it the first `[`.
    obj_start = s.find("{")
    if obj_start < 0:
        return None
    arr_start = s.find("[", obj_start)
    if arr_start < 0:
        return None

    # Walk inside the array, collecting balanced `{...}` elements until we
    # either hit `]` (complete) or run out of input (truncated). Re-assemble
    # a synthetic complete object.
    items: list[Any] = []
    i = arr_start + 1
    n = len(s)
    while i < n:
        # Skip whitespace + commas.
        while i < n and s[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        if s[i] == "]":
            break
        if s[i] != "{":
            # Maybe a primitive — try a small slice.
            break
        # Balanced object scan.
        depth = 0
        in_str = False
        esc = False
        start = i
        j = i
        complete = False
        while j < n:
            c = s[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        chunk = s[start : j + 1]
                        try:
                            items.append(json.loads(chunk))
                        except json.JSONDecodeError:
                            pass
                        complete = True
                        j += 1
                        break
            j += 1
        if not complete:
            break
        i = j

    if not items:
        return None

    # Re-emit a minimal `{key: [items]}` object. We don't know which key
    # the array sat under — try to grab it from the prefix.
    prefix = s[obj_start:arr_start]
    # Look for the most recent `"key":` before the `[`.
    import re as _re

    m = _re.search(r'"([^"]+)"\s*:\s*$', prefix.rstrip())
    array_key = m.group(1) if m else "items"
    return {array_key: items}


def _escape_raw_newlines_in_strings(s: str) -> str:
    """Экранировать переводы строк, оказавшиеся внутри строк JSON.

    Модель пишет связный текст абзацами и иногда ставит между ними живой
    перенос прямо в значении. Для JSON это синтаксическая ошибка, хотя
    ответ полный и по смыслу целый. Раньше такой ответ отбрасывался
    целиком с сообщением «AI returned invalid JSON», по которому нельзя
    догадаться, что данные на самом деле пришли.

    Идём по строке, отслеживая, находимся ли мы внутри строкового литерала,
    и заменяем сырые управляющие символы на их экранированный вид.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in s:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            elif ch == "\n":
                out.append("\\n")
                continue
            elif ch == "\r":
                out.append("\\r")
                continue
            elif ch == "\t":
                out.append("\\t")
                continue
        elif ch == '"':
            in_string = True
        out.append(ch)
    return "".join(out)


async def chat_json(
    *,
    system: str,
    user: str,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 3000,
    json_mode: bool = False,
) -> dict[str, Any]:
    """Chat completion that must return valid JSON. Parses and returns the dict.

    `json_mode=False` is the default after multiple UAT failures: the
    CometAPI-proxied Claude treats `response_format=json_object` as an
    agentic/tool-use signal and hallucinates ``<tool_calls>`` blocks or
    workspace-aware preambles instead of returning the plain JSON we
    asked for. The prompt in each skill is responsible for instructing
    the model to emit ONLY JSON; our recovery pipeline below handles
    markdown fences, prose preambles, and mid-string truncation.

    Recovery pipeline:
      1. Strip ``` ```json ... ``` ``` markdown fences.
      2. Parse the whole cleaned string.
      3. If that fails, extract the first balanced top-level `{...}` or
         `[...]` block and parse it.
      4. If THAT fails, try truncation-recovery: walk an `{"key": [...]}`
         shape and collect every balanced element from the array up to
         the truncation point.
    """
    raw = await chat_completion(
        system=system,
        user=user,
        model=model,
        json_mode=json_mode,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    cleaned = _extract_json_payload(raw)

    # Pass 1: parse as-is.
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}
    except json.JSONDecodeError:
        pass

    # Pass 2: balanced top-level slice.
    slice_ = _balanced_json_slice(cleaned)
    if slice_:
        try:
            parsed = json.loads(slice_)
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"items": parsed}
        except json.JSONDecodeError:
            pass

    # Pass 3: сырые переводы строк внутри значений — частый случай у
    # длинных текстов, где абзацы разделены живым переносом.
    repaired = _escape_raw_newlines_in_strings(cleaned)
    if repaired != cleaned:
        for candidate in (repaired, _balanced_json_slice(repaired) or ""):
            if not candidate:
                continue
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    logger.info("JSON восстановлен экранированием переносов")
                    return parsed
                if isinstance(parsed, list):
                    return {"items": parsed}
            except json.JSONDecodeError:
                pass

    # Pass 4: truncation recovery for `{"key": [...]}` mid-array.
    recovered = _recover_truncated_object(cleaned)
    if recovered:
        return recovered

    logger.error("AI returned invalid JSON. Raw head: %s", raw[:500])
    raise RuntimeError("AI returned invalid JSON (could not recover payload)")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def embed(text: str, model: str | None = None) -> list[float]:
    """Single text → embedding vector. Used for voice_samples retrieval."""
    response = await _client().embeddings.create(
        model=model or settings.COMETAPI_MODEL_EMBEDDING,
        input=text,
    )
    return list(response.data[0].embedding)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def transcribe_audio(local_path, model: str | None = None) -> str:
    """Whisper transcription. local_path is pathlib.Path or str pointing to a
    file on disk small enough for the provider (we chunk callers if not)."""
    with open(local_path, "rb") as f:
        response = await _client().audio.transcriptions.create(
            model=model or settings.COMETAPI_MODEL_WHISPER,
            file=f,
        )
    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("Empty whisper response")
    return text
