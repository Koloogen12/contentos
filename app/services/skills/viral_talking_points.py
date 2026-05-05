"""Extract skill: source content → ranked TalkingPoints (NodeData.extract)."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Node
from app.services import ai_client
from app.services.skills.base import register

SYSTEM_TEMPLATE = """\
{brand_context}

Ты — редактор-аналитик. Из исходного материала извлеки 5–10 «молекулярных тезисов» — \
по одной мысли, каждый самодостаточный, без воды. Под каждый тезис посчитай вирусный \
скор по 4 осям (1–5 каждая):

1. audience_fit — попадание в боль/желание целевой аудитории автора
2. engagement_trigger — есть ли крючок: спор, парадокс, конкретика, эмоция
3. uniqueness — насколько мысль отличается от того, что уже все говорят
4. author_fit — соответствие голосу и убеждениям автора

Итоговый viral_score = сумма (4–20).

ОТВЕТ ВЕРНИ СТРОГО как JSON:
{{
  "talking_points": [
    {{
      "text": "одно предложение, самодостаточный тезис",
      "score_breakdown": {{"audience_fit": 0, "engagement_trigger": 0, "uniqueness": 0, "author_fit": 0}},
      "viral_score": 0,
      "category": "мышление | продукты | ремесло | люди | …",
      "reasoning": "одно предложение почему такой скор"
    }}
  ]
}}

Никаких комментариев вне JSON. Сортируй по viral_score убыв."""

USER_TEMPLATE = """\
ИСХОДНЫЙ МАТЕРИАЛ (до 8000 символов, может быть транскрипт):

{content}"""


@register("viral_talking_points")
async def run(
    db: AsyncSession,
    node: Node,
    system_context: str,
    skill_input: dict[str, Any],
) -> dict[str, Any]:
    content = skill_input.get("source_content") or ""
    if not content.strip():
        raise ValueError("Источник пустой")

    system = SYSTEM_TEMPLATE.format(brand_context=system_context or "Нет brand context.")
    user = USER_TEMPLATE.format(content=content[:8000])

    parsed = await ai_client.chat_json(
        system=system,
        user=user,
        temperature=0.7,
        max_tokens=4000,
    )
    points = parsed.get("talking_points") or []
    if not isinstance(points, list) or not points:
        raise RuntimeError("AI не вернул talking_points")

    cleaned: list[dict[str, Any]] = []
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
        cleaned.append(
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
    cleaned = [p for p in cleaned if p["text"]]
    cleaned.sort(key=lambda p: p["viral_score"], reverse=True)

    new_data = dict(node.data or {})
    new_data["talking_points"] = cleaned
    if new_data.get("selected_index") is None and cleaned:
        new_data["selected_index"] = 0

    return {"node_data": new_data, "meta": {"count": len(cleaned)}}
