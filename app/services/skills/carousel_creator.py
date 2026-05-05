"""Format skill: talking_point → Instagram/LinkedIn carousel (5–10 slides)."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Node
from app.services import ai_client
from app.services.skills.base import register

SYSTEM_TEMPLATE = """\
{brand_context}

Ты собираешь карусель из 5–10 слайдов на основе одного тезиса. Принципы:
- Слайд 1 — обложка-хук: одна короткая строка + подзаголовок (что внутри / для кого).
- Слайды 2..N-1 — по одной мысли на слайд: title (до 60 символов) + body (до 35 слов).
- Последний слайд — итог + CTA.
- Один тезис на слайд. Никакого «продолжения мысли на следующий слайд».
- Не использовать эмодзи, хэштеги, общие фразы («это очень важно»).

ОТВЕТ СТРОГО как JSON:
{{
  "slides": [
    {{"title": "обложка-хук", "body": "подзаголовок", "is_cover": true}},
    {{"title": "...", "body": "..."}}
  ],
  "summary": "одно предложение что в карусели",
  "cta": "одно предложение"
}}"""

USER_TEMPLATE = """\
ТЕЗИС:
{talking_point}

Собери карусель."""


def _flatten(slides: list[dict]) -> str:
    parts: list[str] = []
    for i, s in enumerate(slides, 1):
        parts.append(f"[Slide {i}] {s.get('title','')}\n{s.get('body','')}")
    return "\n\n".join(parts).strip()


@register("carousel_creator")
async def run(
    db: AsyncSession,
    node: Node,
    system_context: str,
    skill_input: dict[str, Any],
) -> dict[str, Any]:
    tp = (skill_input.get("talking_point") or "").strip()
    if not tp:
        raise ValueError("Нет входного тезиса")

    system = SYSTEM_TEMPLATE.format(brand_context=system_context or "Нет brand context.")
    user = USER_TEMPLATE.format(talking_point=tp)

    parsed = await ai_client.chat_json(system=system, user=user, temperature=0.8, max_tokens=3000)

    slides_raw = parsed.get("slides") or []
    slides: list[dict[str, Any]] = []
    for s in slides_raw:
        if not isinstance(s, dict):
            continue
        title = str(s.get("title", "")).strip()
        body = str(s.get("body", "")).strip()
        if not title and not body:
            continue
        slides.append(
            {
                "title": title,
                "body": body,
                "is_cover": bool(s.get("is_cover", False)),
            }
        )
    if len(slides) < 3:
        raise RuntimeError("AI вернул слишком мало слайдов")

    summary = str(parsed.get("summary", "")).strip()
    cta = str(parsed.get("cta", "")).strip()

    new_data = dict(node.data or {})
    new_data.update(
        {
            "platform": "carousel",
            "talking_point_text": tp,
            "slides": slides,
            "summary": summary,
            "cta": cta,
            "full_text": _flatten(slides) + (f"\n\n{cta}" if cta else ""),
        }
    )
    return {"node_data": new_data, "meta": {"slides_count": len(slides)}}
