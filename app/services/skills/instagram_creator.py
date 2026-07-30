"""Format skill: talking_point → Instagram caption + visual notes.

Distinct from carousel_creator (slides) and reels_creator (script): this
generates a single-post caption with visual direction notes for a static
image / photo carousel post.
"""
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

Ты пишешь Instagram пост (статичный single-post или фото с подписью). Не carousel \
и не Reels — для них есть отдельные скиллы.

Структура подписи:
1. Первая строка (до 125 символов) — хук, видим до «...подробнее».
2. Тело — 80–250 слов, абзацами через пустую строку. Без эмодзи.
3. CTA — короткий призыв к комментарию или сохранению.
4. Подсказка по визуалу — что показать на фото / что в кадре. Один-два предложения.

{voice_rule}

ОТВЕТ СТРОГО как JSON:
{{
  "hook": "первая строка до 125 символов",
  "caption": "полная подпись (включает хук + тело + CTA)",
  "body": "только тело без хука и CTA",
  "cta": "одно предложение",
  "visual_direction": "что в кадре"
}}"""

USER_TEMPLATE = """\
ТЕЗИС:
{talking_point}

Напиши Instagram пост."""


@register("instagram_creator")
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

    parsed = await ai_client.chat_json(
        system=system, user=user, temperature=0.8, max_tokens=2000
    )

    hook = strip_meta_offers(str(parsed.get("hook", "")))
    caption = strip_meta_offers(str(parsed.get("caption", "")))
    body = strip_meta_offers(str(parsed.get("body", "")))
    cta = strip_meta_offers(str(parsed.get("cta", "")))
    visual = strip_meta_offers(str(parsed.get("visual_direction", "")))
    if not caption and (hook or body):
        caption = "\n\n".join(p for p in (hook, body, cta) if p)
    if not caption:
        raise RuntimeError("AI не вернул caption")

    new_data = dict(node.data or {})
    new_data.update(
        {
            "platform": "instagram",
            "talking_point_text": tp,
            "hook": hook,
            "body": body,
            "cta": cta,
            "caption": caption,
            "visual_direction": visual,
            "full_text": caption + (f"\n\n---\nКадр: {visual}" if visual else ""),
        }
    )
    return {"node_data": new_data, "meta": {}}
