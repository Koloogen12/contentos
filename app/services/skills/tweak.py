"""Tweak skill — re-runs an existing node's data with a transformation mode.

Used by the secondary actions panel: Усилить / Сократить / Перефразировать
on extract, Перегенерировать / Другой хук / Сократить / Усилить голос /
Под платформу on format.

The mode is the only knob; the skill picks the right system prompt and
mutates only the fields that should change (e.g. "shorten" rewrites body +
full_text but keeps hooks). Everything else is preserved.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Node
from app.services import ai_client
from app.services.skills.base import MACHINE_TELLS_BLOCK, register

# === Extract tweaks ===============================================

_EXTRACT_AMPLIFY = """\
{brand_context}

У тебя есть готовый список тезисов. Усиль ИХ ЖЕ — сделай каждый острее, \
конкретнее, провокационнее, но НЕ меняй смысл. Тот же набор тем, тот же \
порядок. Только переформулируй сильнее.

Ответ — JSON в том же формате что и исходный список:
{{"talking_points": [{{"text": "...", "score_breakdown": {{"audience_fit":0,"engagement_trigger":0,"uniqueness":0,"author_fit":0}}, "viral_score": 0, "category": "...", "reasoning": "..."}}]}}
"""

_EXTRACT_REPHRASE = """\
{brand_context}

Перефразируй каждый тезис другими словами, сохранив суть. Меняй \
конструкцию, лексику, порядок частей предложения — но не сам смысл.
Тот же набор тем и порядок. Сохрани score_breakdown / viral_score / category.

Ответ строго JSON:
{{"talking_points": [{{"text": "...", "score_breakdown": {{...}}, "viral_score": N, "category": "...", "reasoning": "..."}}]}}
"""

_EXTRACT_REEXTRACT = """\
{brand_context}

Извлеки ДРУГИЕ тезисы из этого же материала. Те что были — отбрось, \
найди новые углы и идеи. 5–10 тезисов со скором 4–20 (audience_fit + \
engagement_trigger + uniqueness + author_fit).

Ответ строго JSON:
{{"talking_points": [{{"text": "...", "score_breakdown": {{...}}, "viral_score": N, "category": "...", "reasoning": "..."}}]}}
"""


# === Format tweaks ================================================

_FORMAT_REGEN = """\
{brand_context}

Перегенерируй пост целиком на основе тезиса. Новые хуки, новый body, \
новый CTA. Платформа та же. Структура — как у исходного.
"""

_FORMAT_REHOOK = """\
{brand_context}

Сохрани body и CTA без изменений. Сгенерируй ТОЛЬКО 3 новых хука для \
этого тезиса. Каждый — самодостаточный, до 90–110 символов.

Ответ строго JSON: {{"hooks": ["...", "...", "..."]}}
"""

# Carousel rehook = переписать слайд-обложку (slide[0], is_cover). Для IG
# карусели именно обложка — главный hook (то что видно в ленте до клика).
_FORMAT_REHOOK_CAROUSEL = """\
{brand_context}

У готовой карусели нужно сменить ТОЛЬКО обложку (первый слайд). Остальные \
слайды и подпись (caption) трогать нельзя. Дай 3 разных варианта обложки \
(title до 60 символов + body — короткий подзаголовок про «что внутри / \
для кого / листай →»). Отбирай по интриге, остановке скролла, обещанию \
конкретной пользы.

Ответ строго JSON:
{{"cover_variants": [{{"title": "...", "body": "..."}}, {{"title": "...", "body": "..."}}, {{"title": "...", "body": "..."}}]}}
"""

# Reels rehook — пересоздать 3 hook-варианта первой секунды (0–3 сек).
_FORMAT_REHOOK_REELS = """\
{brand_context}

У готового сценария рилса остаются beats и CTA. Дай 3 новых HOOK-варианта \
для первых 0–3 секунд — короткие (до 10 слов), которые остановят скролл \
сразу. Beats и caption не трогай.

Ответ строго JSON: {{"hooks": ["...", "...", "..."]}}
"""

# Twitter rehook — первый твит = hook (в single OR thread). Перегенерируем
# его в 3 вариантах, остальные tweets[] остаются.
_FORMAT_REHOOK_TWITTER = """\
{brand_context}

У готового X/Twitter поста (single или начало треда) пересоздай ТОЛЬКО \
первый твит — три новых варианта. Каждый ≤ 280 символов, без хэштегов, \
без эмодзи. Остальные твиты (если тред) не трогай.

Ответ строго JSON: {{"hooks": ["вариант 1", "вариант 2", "вариант 3"]}}
"""

# Article rehook — у статьи есть отдельные `hook` (одно предложение перед
# intro) и `title`. Перегенерируем оба.
_FORMAT_REHOOK_ARTICLE = """\
{brand_context}

У готовой статьи пересоздай заголовок и hook (первая строка под title), \
сохрани intro / sections / conclusion / cta / meta как есть. Заголовок \
до 70 символов, hook до 110 символов.

Ответ строго JSON: {{"title": "...", "hook": "..."}}
"""

# Hooks-bank это сам банк хуков — rehook = переотбор N новых хуков.
_FORMAT_REHOOK_HOOKS_BANK = """\
{brand_context}

У тебя был банк хуков для этого тезиса. Перегенерируй ВЕСЬ банк — 5–10 \
НОВЫХ хуков, других чем были. Сохрани разнообразие триггеров (paradox / \
number / contrast / provocation / story / dissonance / question).

Ответ строго JSON:
{{"hooks_bank": [{{"text": "...", "trigger": "..."}}, ...]}}
"""

_FORMAT_SHORTEN = """\
{brand_context}

Сократи body на 30–40% без потери смысла. Удали воду, повторы, \
смягчающие конструкции. Хуки и CTA не трогай.

Ответ строго JSON: {{"body": "..."}}
"""

_FORMAT_AMPLIFY_VOICE = """\
{brand_context}

Перепиши тот же body, усиливая голос автора: чаще короткие фразы, \
больше характерных оборотов из voice_traits / recurring_phrases / \
brand_voice. Не добавляй новых аргументов — только подгони регистр.

Ответ строго JSON: {{"body": "..."}}
"""

_FORMAT_EDIT = """\
{brand_context}

Отредактируй body по методу «сначала смысл, потом слова». Проходы идут \
последовательно, от крупного к мелкому — не начинай чинить слова, пока не \
проверил смысл.

1. Смысл. Голословные утверждения, оценки вместо фактов, неопределённость \
   («многие», «всё больше»). Ничего не выдумывай: если фактуры не хватает, \
   формулировку смягчи или убери, но не добирай правдоподобной выдумкой.
2. Тональность. Никаких «вы должны» и нравоучений. Читатель не дурак \
   и не лентяй.
3. Предложение. Люди и действия вместо явлений и процессов. Причастия \
   и деепричастия переформулируй. Не больше двух-трёх сущностей на \
   предложение. Страдательный залог — в активный.
4. Слово. Канцелярит, штампы, заумь, усилители. Проверка: если слово \
   нельзя сказать вслух за чаем — выкидывай.
5. Ритм. Перечитай про себя. После сокращений предложения становятся \
   одинаково рублеными — чередуй длину.
6. Приметы машины. Обязательный финальный проход по блоку выше.

ЧЕГО НЕ ДЕЛАТЬ:
- Не менять смысл. Правка усиливает текст автора, а не заменяет его \
  текстом редактора.
- Не пересушивать. Цель не самый короткий текст, а самый полезный. \
  Мусор убирать, детали, примеры и живость — нет.
- Не выдумывать факты, цифры и примеры.

Хуки и CTA не трогай — их правят отдельными режимами.

{machine_tells}

Ответ строго JSON: {{"body": "..."}}
"""

_FORMAT_PLATFORM = """\
{brand_context}

Адаптируй пост под особенности платформы: длину, плотность абзацев, \
уровень формальности. Сохрани смысл, перепиши body. Хуки и CTA \
оставь как есть.

Ответ строго JSON: {{"body": "..."}}
"""


def _normalize_extract(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    points = parsed.get("talking_points") or []
    out: list[dict[str, Any]] = []
    for p in points:
        if not isinstance(p, dict):
            continue
        sb = p.get("score_breakdown") or {}
        score = p.get("viral_score")
        if not isinstance(score, int):
            try:
                score = int(
                    sum(int(sb.get(k, 0)) for k in ("audience_fit", "engagement_trigger", "uniqueness", "author_fit"))
                )
            except (TypeError, ValueError):
                score = 0
        out.append(
            {
                "text": str(p.get("text", "")).strip(),
                "score_breakdown": {
                    "audience_fit": int(sb.get("audience_fit", 0)),
                    "engagement_trigger": int(sb.get("engagement_trigger", 0)),
                    "uniqueness": int(sb.get("uniqueness", 0)),
                    "author_fit": int(sb.get("author_fit", 0)),
                },
                "viral_score": score,
                "category": str(p.get("category", "")).strip(),
                "reasoning": str(p.get("reasoning", "")).strip(),
            }
        )
    return [p for p in out if p["text"]]


@register("tweak")
async def run(
    db: AsyncSession,
    node: Node,
    system_context: str,
    skill_input: dict[str, Any],
) -> dict[str, Any]:
    mode = (skill_input.get("mode") or "").strip()
    current = dict(node.data or {})
    new_data = dict(current)

    brand = system_context or "Нет brand context."

    # ----- extract tweaks -----
    if node.type == "extract":
        if mode not in ("amplify", "rephrase", "reextract"):
            raise ValueError(f"Неизвестный mode для extract: {mode!r}")

        # Build prompt context: existing list + (for reextract) the source content too
        existing = current.get("talking_points") or []
        source_text = ""
        if mode == "reextract":
            source_text = skill_input.get("source_content") or ""

        system_map = {
            "amplify": _EXTRACT_AMPLIFY,
            "rephrase": _EXTRACT_REPHRASE,
            "reextract": _EXTRACT_REEXTRACT,
        }
        system = system_map[mode].format(brand_context=brand)

        if mode == "reextract":
            user = f"ИСХОДНЫЙ МАТЕРИАЛ:\n{source_text[:8000]}"
        else:
            import json
            user = "ТЕКУЩИЕ ТЕЗИСЫ (JSON):\n" + json.dumps(existing, ensure_ascii=False)

        parsed = await ai_client.chat_json(
            system=system, user=user, temperature=0.7, max_tokens=4000
        )
        points = _normalize_extract(parsed)
        if not points:
            raise RuntimeError("AI вернул пустой список")
        # Sort by score for amplify/reextract; preserve order on rephrase
        if mode in ("amplify", "reextract"):
            points.sort(key=lambda p: p["viral_score"], reverse=True)
        new_data["talking_points"] = points
        if new_data.get("selected_index") is None or new_data["selected_index"] >= len(points):
            new_data["selected_index"] = 0
        return {"node_data": new_data, "meta": {"mode": mode, "count": len(points)}}

    # ----- format tweaks -----
    if node.type == "format":
        platform = current.get("platform", "telegram")
        tp = current.get("talking_point_text") or skill_input.get("talking_point") or ""
        if not tp:
            raise ValueError("Нет talking_point для tweak format")

        if mode == "regenerate":
            # Delegate to the platform's primary creator (re-run skill).
            from app.services.skills.base import FORMAT_PLATFORM_TO_SKILL, get
            primary = FORMAT_PLATFORM_TO_SKILL.get(platform)
            if not primary:
                raise ValueError(f"Платформа {platform} не поддерживается")
            primary_fn = get(primary)
            return await primary_fn(db, node, system_context, {"talking_point": tp, "platform": platform})

        if mode == "rehook":
            return await _rehook_by_platform(
                platform=platform,
                tp=tp,
                brand=brand,
                current=current,
                new_data=new_data,
            )

        # Условие входа выводится из самого словаря, а не дублирует его
        # списком: раньше «edit» добавили в словарь, забыли в условие, и
        # режим падал с «неизвестный mode» при том, что промпт для него был.
        system_map = {
            "shorten": _FORMAT_SHORTEN,
            "amplify_voice": _FORMAT_AMPLIFY_VOICE,
            "platform_optimize": _FORMAT_PLATFORM,
            "edit": _FORMAT_EDIT,
        }
        if mode in system_map:
            # machine_tells нужен только режиму «Редактура»; остальным
            # промптам лишний kwarg безвреден — str.format его игнорирует.
            system = system_map[mode].format(
                brand_context=brand, machine_tells=MACHINE_TELLS_BLOCK
            )
            # Редактура обязана вернуть текст целиком, а не сжать его, поэтому
            # ей и на вход, и на выход нужен запас: длинная статья в 3000
            # символов не помещается, а урезанный ответ выглядел бы как
            # «редактор проглотил половину материала».
            is_edit = mode == "edit"
            body_in = current.get("body", "")[: 12000 if is_edit else 3000]
            user = f"ТЕЗИС: {tp}\nПЛАТФОРМА: {platform}\n\nТЕКУЩИЙ BODY: {body_in}"
            parsed = await ai_client.chat_json(
                system=system,
                user=user,
                temperature=0.7,
                max_tokens=8000 if is_edit else 2000,
            )
            new_body = str(parsed.get("body", "")).strip()
            if not new_body:
                raise RuntimeError("AI не вернул body")
            new_data["body"] = new_body
            hooks = current.get("hooks") or []
            selected = current.get("selected_hook_index", 0)
            hook = hooks[selected] if hooks and 0 <= selected < len(hooks) else ""
            new_data["full_text"] = _assemble_full_text(hook, new_body, current.get("cta", ""))
            return {"node_data": new_data, "meta": {"mode": mode}}

        raise ValueError(f"Неизвестный mode для format: {mode!r}")

    raise ValueError(f"Tweak не поддерживает тип ноды {node.type}")


def _assemble_full_text(hook: str, body: str, cta: str) -> str:
    return "\n\n".join(p.strip() for p in (hook, body, cta) if p and p.strip())


async def _rehook_by_platform(
    *,
    platform: str,
    tp: str,
    brand: str,
    current: dict[str, Any],
    new_data: dict[str, Any],
) -> dict[str, Any]:
    """Platform-aware rehook.

    Each format has a different "hook surface":
      - telegram / linkedin / instagram → hooks[] (radio in PostBody)
      - carousel → slides[0] (cover slide — that's the real IG hook)
      - reels → hooks[] (0–3 sec opener)
      - twitter → tweets[0] (head of thread or the whole single tweet)
      - article → title + hook (one-liner under title)
      - hooks (the hooks-bank format itself) → hooks_bank[] (regen the bank)

    We branch here so the user's click on "Другой хук" actually changes the
    visible surface for their platform, instead of writing into a field
    the UI doesn't render (the carousel bug that triggered this fix).
    """
    if platform == "carousel":
        system = _FORMAT_REHOOK_CAROUSEL.format(brand_context=brand)
        user = (
            f"ТЕЗИС: {tp}\n\n"
            f"ТЕКУЩАЯ ОБЛОЖКА:\n"
            f"title: {(current.get('slides') or [{}])[0].get('title', '') if current.get('slides') else ''}\n"
            f"body: {(current.get('slides') or [{}])[0].get('body', '') if current.get('slides') else ''}"
        )
        parsed = await ai_client.chat_json(
            system=system, user=user, temperature=0.9, max_tokens=1200
        )
        variants = parsed.get("cover_variants") or []
        clean = []
        for v in variants:
            if not isinstance(v, dict):
                continue
            t = str(v.get("title", "")).strip()
            b = str(v.get("body", "")).strip()
            if t:
                clean.append({"title": t, "body": b, "is_cover": True})
        if not clean:
            raise RuntimeError("AI не вернул варианты обложки")
        # Apply the first variant; stash the rest in meta so a future
        # "Другой вариант" button can rotate through them without an extra
        # AI call. We treat slides[0] as the cover and replace it.
        slides = list(current.get("slides") or [])
        chosen = clean[0]
        if slides:
            slides[0] = chosen
        else:
            slides = [chosen]
        new_data["slides"] = slides
        new_data["cover_variants"] = clean  # 3 alternatives surface in UI
        # Re-assemble full_text the same way carousel_creator does it.
        from app.services.skills.carousel_creator import _flatten
        full = _flatten(slides)
        summary = current.get("summary", "")
        cta = current.get("cta", "")
        kw = current.get("comment_keyword")
        parts = [full]
        if summary:
            parts.append(f"--- caption ---\n{summary}")
        if cta:
            parts.append(cta)
        if kw:
            parts.append(f'Напиши в комментариях «{kw}» — пришлю в Direct.')
        new_data["full_text"] = "\n\n".join(p for p in parts if p)
        return {"node_data": new_data, "meta": {"mode": "rehook", "variants": len(clean)}}

    if platform == "reels":
        system = _FORMAT_REHOOK_REELS.format(brand_context=brand)
        user = f"ТЕЗИС: {tp}\n\nТЕКУЩИЕ BEATS: {len(current.get('beats') or [])} штук"
        parsed = await ai_client.chat_json(
            system=system, user=user, temperature=0.9, max_tokens=800
        )
        hooks = [str(h).strip() for h in (parsed.get("hooks") or []) if str(h).strip()]
        if not hooks:
            raise RuntimeError("AI не вернул hooks")
        new_data["hooks"] = hooks
        new_data["selected_hook_index"] = 0
        # full_text for reels uses the assembly from reels_script_writer
        from app.services.skills.reels_script_writer import _format_full_text
        new_data["full_text"] = _format_full_text(
            hooks[0],
            current.get("beats") or [],
            current.get("cta", ""),
            current.get("caption", ""),
        )
        return {"node_data": new_data, "meta": {"mode": "rehook", "hooks_count": len(hooks)}}

    if platform == "twitter":
        system = _FORMAT_REHOOK_TWITTER.format(brand_context=brand)
        user = (
            f"ТЕЗИС: {tp}\n\nТЕКУЩИЙ ПЕРВЫЙ ТВИТ: "
            f"{(current.get('tweets') or [''])[0][:300]}"
        )
        parsed = await ai_client.chat_json(
            system=system, user=user, temperature=0.9, max_tokens=800
        )
        variants = [
            str(h).strip() for h in (parsed.get("hooks") or []) if str(h).strip()
        ]
        if not variants:
            raise RuntimeError("AI не вернул варианты")
        tweets = list(current.get("tweets") or [])
        if tweets:
            tweets[0] = variants[0]
        else:
            tweets = [variants[0]]
        new_data["tweets"] = tweets
        new_data["hook"] = variants[0]
        # Stash the alts so user can cycle.
        new_data["hooks"] = variants
        new_data["selected_hook_index"] = 0
        new_data["full_text"] = "\n\n".join(t for t in tweets if t.strip())
        return {"node_data": new_data, "meta": {"mode": "rehook", "variants": len(variants)}}

    if platform == "article":
        system = _FORMAT_REHOOK_ARTICLE.format(brand_context=brand)
        user = (
            f"ТЕЗИС: {tp}\n\n"
            f"ТЕКУЩИЙ TITLE: {current.get('title', '')[:200]}\n"
            f"ТЕКУЩИЙ HOOK: {current.get('hook', '')[:200]}"
        )
        parsed = await ai_client.chat_json(
            system=system, user=user, temperature=0.85, max_tokens=600
        )
        new_title = str(parsed.get("title", "")).strip()
        new_hook = str(parsed.get("hook", "")).strip()
        if not (new_title or new_hook):
            raise RuntimeError("AI не вернул title/hook")
        if new_title:
            new_data["title"] = new_title
        if new_hook:
            new_data["hook"] = new_hook
        # Reassemble article markdown via the same helper as article_creator
        from app.services.skills.article_creator import _assemble_markdown
        new_data["full_text"] = _assemble_markdown(new_data)
        return {"node_data": new_data, "meta": {"mode": "rehook"}}

    if platform == "hooks":
        system = _FORMAT_REHOOK_HOOKS_BANK.format(brand_context=brand)
        user = f"ТЕЗИС: {tp}"
        parsed = await ai_client.chat_json(
            system=system, user=user, temperature=0.9, max_tokens=1500
        )
        raw = parsed.get("hooks_bank") or parsed.get("hooks") or []
        bank: list[dict[str, str]] = []
        for h in raw:
            if isinstance(h, dict):
                text = str(h.get("text", "")).strip()
                if text:
                    bank.append(
                        {
                            "text": text,
                            "trigger": str(h.get("trigger", "")).strip() or "other",
                        }
                    )
            elif isinstance(h, str) and h.strip():
                bank.append({"text": h.strip(), "trigger": "other"})
        if not bank:
            raise RuntimeError("AI не вернул банк хуков")
        new_data["hooks_bank"] = bank
        new_data["full_text"] = "\n\n".join(
            f"{i + 1}. {b['text']} ({b['trigger']})" for i, b in enumerate(bank)
        )
        return {"node_data": new_data, "meta": {"mode": "rehook", "count": len(bank)}}

    # Default: telegram / linkedin / instagram → hooks[] + assemble post.
    system = _FORMAT_REHOOK.format(brand_context=brand)
    user = f"ТЕЗИС: {tp}\n\nТЕКУЩИЙ BODY: {current.get('body', '')[:2000]}"
    parsed = await ai_client.chat_json(
        system=system, user=user, temperature=0.9, max_tokens=800
    )
    hooks = [str(h).strip() for h in (parsed.get("hooks") or []) if str(h).strip()]
    if not hooks:
        raise RuntimeError("AI не вернул hooks")
    new_data["hooks"] = hooks
    new_data["selected_hook_index"] = 0
    new_data["full_text"] = _assemble_full_text(
        hooks[0], current.get("body", ""), current.get("cta", "")
    )
    return {"node_data": new_data, "meta": {"mode": "rehook", "hooks_count": len(hooks)}}
