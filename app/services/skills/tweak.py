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
from app.services.skills.base import register

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
            return {"node_data": new_data, "meta": {"mode": mode, "hooks_count": len(hooks)}}

        if mode in ("shorten", "amplify_voice", "platform_optimize"):
            system_map = {
                "shorten": _FORMAT_SHORTEN,
                "amplify_voice": _FORMAT_AMPLIFY_VOICE,
                "platform_optimize": _FORMAT_PLATFORM,
            }
            system = system_map[mode].format(brand_context=brand)
            user = f"ТЕЗИС: {tp}\nПЛАТФОРМА: {platform}\n\nТЕКУЩИЙ BODY: {current.get('body', '')[:3000]}"
            parsed = await ai_client.chat_json(
                system=system, user=user, temperature=0.7, max_tokens=2000
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
