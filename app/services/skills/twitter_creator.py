"""Format skill: talking_point → X/Twitter post (single tweet OR thread)."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Node
from app.services import ai_client
from app.services.skills.base import register

SYSTEM_TEMPLATE = """\
{brand_context}

Ты пишешь для X / Twitter на основе одного тезиса. Решаешь сам — один пост (если \
влезает в 280 символов с воздухом) или тред (2–7 твитов).

Принципы:
- Каждый твит — 200–280 символов. Без хэштегов. Без эмодзи.
- Первый твит — хук. Должен останавливать скролл сам по себе, без контекста.
- Если тред — нумерация 1/N в конце каждого твита.
- Последний твит — конкретный вывод или вопрос. Не «follow me».

Стиль X: ёмкий, парадоксальный, конкретный. Хорошо заходят: цифры, контраст, \
неочевидный угол, личный опыт в одной строке.

ОТВЕТ СТРОГО как JSON:
{{
  "format": "single" /* или "thread" */,
  "tweets": ["текст первого твита", "текст второго", ...],
  "hook": "первая строка треда / single tweet",
  "cta": "последний твит/призыв"
}}"""

USER_TEMPLATE = """\
ТЕЗИС:
{talking_point}

Напиши пост / тред."""


@register("twitter_creator")
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

    parsed = await ai_client.chat_json(
        system=system, user=user, temperature=0.85, max_tokens=2000
    )

    tweets_raw = parsed.get("tweets") or []
    tweets = [str(t).strip() for t in tweets_raw if str(t).strip()]
    if not tweets:
        raise RuntimeError("AI не вернул tweets")

    fmt = str(parsed.get("format", "single")).strip()
    if fmt not in ("single", "thread"):
        fmt = "single" if len(tweets) == 1 else "thread"

    hook = str(parsed.get("hook", "")).strip() or tweets[0]
    cta = str(parsed.get("cta", "")).strip()

    full_text = "\n\n".join(tweets)

    new_data = dict(node.data or {})
    new_data.update(
        {
            "platform": "twitter",
            "talking_point_text": tp,
            "format_type": fmt,
            "tweets": tweets,
            "hook": hook,
            "cta": cta,
            "full_text": full_text,
        }
    )
    return {"node_data": new_data, "meta": {"format": fmt, "tweets_count": len(tweets)}}
