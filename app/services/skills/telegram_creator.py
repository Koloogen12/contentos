"""Format skill: talking_point → publishable Telegram post."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Node
from app.services import ai_client
from app.services.skills.base import (
    OUTPUT_LANGUAGE_DIRECTIVE,
    VOICE_RULE_BLOCK,
    register,
    strip_meta_offers,
)

SYSTEM_TEMPLATE = """\
{brand_context}

{language_directive}

Ты пишешь пост для Telegram-канала на основе одного тезиса. Формат:
1. Хук-первая строка (до 90 символов, чтобы влез в превью). Сделай 3 разных варианта хука.
2. Тело поста: 80–250 слов, абзацами по 1–3 строки. Без эмодзи. Без хэштегов. Без заголовков.
3. CTA — короткая, конкретная, не «подписывайся».

Стиль Telegram: разговорный, плотный, без воды. Можно личные истории, конкретные числа, цитаты.

ТИПОГРАФИКА И ФОРМАТИРОВАНИЕ — обязательно (Telegram HTML, parse_mode=HTML):
- Жирный — выделяй ключевые тезисы, имена, цифры, ВЫВОД: <b>текст</b>
- Курсив — оттеночные комментарии, реплики, маркер мысли: <i>текст</i>
- Зачёркивание — для отброшенных вариантов / «было vs стало»: <s>текст</s>
- Спойлер (скрытый текст) — точечно, для пуант / усиления интриги: <tg-spoiler>текст</tg-spoiler>
- Цитата — для чужой прямой речи или внутренних мыслей: <blockquote>текст</blockquote>
- Моноширинный — для названий продуктов / команд / коротких терминов: <code>текст</code>

Правила вёрстки:
- Жирным — 2-4 фрагмента на пост (не больше, иначе расфокус). Минимум один жирный фрагмент в хуке или в первом абзаце.
- Курсивом — 1-3 фрагмента. Усиливает голос, не заменяет жирный.
- Спойлеры — максимум 1 на пост, в кульминации.
- Цитаты — только если действительно есть чужая речь / диалог.
- НИКАКИХ эмодзи. НИКАКИХ markdown-символов вроде ** или __. ТОЛЬКО HTML-теги выше.
- Все остальные символы `<` и `>` и `&` в тексте экранируй в `&lt;`, `&gt;`, `&amp;` (если они есть в исходнике).

{voice_rule}

ОТВЕТ СТРОГО как JSON. Поля `body` и `hooks` содержат HTML-разметку как описано выше:
{{
  "hooks": ["вариант 1 с <b>акцентами</b>", "вариант 2", "вариант 3"],
  "selected_hook_index": 0,
  "body": "основной текст с разметкой <b>тут</b>, <i>тут</i>, <tg-spoiler>тут</tg-spoiler>",
  "cta": "одно предложение, можно с <b>акцентом</b>"
}}"""

USER_TEMPLATE = """\
ТЕЗИС:
{talking_point}

Напиши пост."""


def _assemble_full_text(hook: str, body: str, cta: str) -> str:
    parts = [p.strip() for p in (hook, body, cta) if p and p.strip()]
    return "\n\n".join(parts)


@register("telegram_creator")
async def run(
    db: AsyncSession,
    node: Node,
    system_context: str,
    skill_input: dict[str, Any],
) -> dict[str, Any]:
    tp = (skill_input.get("talking_point") or "").strip()
    if not tp:
        raise ValueError("Нет входного тезиса")

    system = SYSTEM_TEMPLATE.format(
        brand_context=system_context or "Нет brand context.",
        language_directive=OUTPUT_LANGUAGE_DIRECTIVE,
        voice_rule=VOICE_RULE_BLOCK,
    )
    user = USER_TEMPLATE.format(talking_point=tp)

    parsed = await ai_client.chat_json(system=system, user=user, temperature=0.8, max_tokens=2500)

    hooks_raw = parsed.get("hooks") or []
    hooks = [strip_meta_offers(str(h)) for h in hooks_raw if str(h).strip()]
    hooks = [h for h in hooks if h]
    if not hooks:
        raise RuntimeError("AI не вернул hooks")

    selected = parsed.get("selected_hook_index", 0)
    if not isinstance(selected, int) or selected < 0 or selected >= len(hooks):
        selected = 0

    body = strip_meta_offers(str(parsed.get("body", "")))
    cta = strip_meta_offers(str(parsed.get("cta", "")))
    if not body:
        raise RuntimeError("AI не вернул body")

    full_text = _assemble_full_text(hooks[selected], body, cta)

    new_data = dict(node.data or {})
    new_data.update(
        {
            "platform": "telegram",
            "talking_point_text": tp,
            "hooks": hooks,
            "selected_hook_index": selected,
            "body": body,
            "cta": cta,
            "full_text": full_text,
        }
    )
    return {"node_data": new_data, "meta": {"hooks_count": len(hooks)}}
