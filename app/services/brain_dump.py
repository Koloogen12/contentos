"""Brain-dump → tezis-bank parser.

User flow: founder pastes a raw thought / paragraph / voice-memo
transcript / chat-message into a textarea. We parse it into 3-10
self-contained `KnowledgeItem` candidates, ready to be pinned to the
tezis-bank and used as carousel / Telegram / LinkedIn input later.

Key differences from `viral_talking_points` (which expects long source
content, like a YouTube transcript):

  - Allowed minimum input is one sentence ("AI заменит фронтендеров?").
    We let the AI EXPAND on the seed rather than only extract from it.
  - No `score_breakdown` — keep the JSON shape compact, score is a
    single integer 1-20.
  - Returned items aren't saved automatically — the API hands them back
    to the caller as proposals, and the user picks which ones to commit
    to the tezis bank.
"""
from __future__ import annotations

from typing import Any

from app.services import ai_client
from app.services.skills.base import (
    OUTPUT_LANGUAGE_DIRECTIVE,
    VOICE_RULE_BLOCK,
    strip_meta_offers,
)


_SYSTEM_TEMPLATE = """\
{brand_context}

{language_directive}

Ты — редактор-куратор. Тебе дают «brain dump» автора: одну мысль, \
обрывок размышлений, заметку из чата или короткую идею. Твоя задача — \
разобрать это на 3–7 самостоятельных тезисов для тезис-банка. Каждый \
тезис должен быть готов превратиться в отдельный пост или карусель.

ПРАВИЛА:
- 3–7 тезисов. Меньше — если идея уже одна-единственная острая мысль, \
  больше — если в brain dump несколько разных мыслей.
- Каждый тезис: одно предложение, самодостаточный, без воды.
- Можно РАЗВЕРНУТЬ исходную мысль с разных углов: контр-аргумент, \
  follow-up, конкретный кейс, мета-уровень. Не ограничивайся буквальным \
  пересказом.
- viral_score 1–20: чем выше, тем больше шанс залететь у целевой \
  аудитории автора (фаундеры, продакты, индихакеры).
- pillar: R1 (продукт / AI-инструменты), R2 (психология фаундера), \
  R3 (реалити-путь к $1M MRR), R4 (разборы рынка). null если не подходит.

{voice_rule}

ОТВЕТ СТРОГО как JSON:
{{
  "tezis": [
    {{
      "title": "одно предложение, до 120 символов, главная мысль",
      "body": "1–3 предложения, чуть подробнее: контекст или развитие мысли",
      "viral_score": 0,
      "pillar": "R1" /* или R2/R3/R4/null */,
      "tags": ["тег1", "тег2"]
    }}
  ]
}}

Сортируй по viral_score убыв. Никаких комментариев вне JSON."""


_USER_TEMPLATE = """\
BRAIN DUMP АВТОРА:

{text}

Разбери на тезисы."""


async def parse_brain_dump(
    *,
    system_context: str,
    text: str,
) -> list[dict[str, Any]]:
    """Parse free-form text into a list of tezis candidates.

    Returns a list of dicts shaped like the `KnowledgeItem` model (title,
    body, viral_score, pillar, tags) but NOT persisted. The API handler
    decides what to save based on user selection.
    """
    text = (text or "").strip()
    if len(text) < 5:
        raise ValueError("Слишком короткий brain dump — нужно хотя бы 5 символов")

    system = _SYSTEM_TEMPLATE.format(
        brand_context=system_context or "Нет brand context.",
        language_directive=OUTPUT_LANGUAGE_DIRECTIVE,
        voice_rule=VOICE_RULE_BLOCK,
    )
    user = _USER_TEMPLATE.format(text=text[:8000])

    parsed = await ai_client.chat_json(
        system=system,
        user=user,
        temperature=0.75,
        max_tokens=3000,
    )

    raw_items = parsed.get("tezis") or []
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        title = strip_meta_offers(str(raw.get("title") or "").strip())
        body = strip_meta_offers(str(raw.get("body") or "").strip())
        if not title and not body:
            continue
        score = raw.get("viral_score")
        if not isinstance(score, int) or score < 0:
            score = 0
        score = max(0, min(20, score))
        pillar_raw = raw.get("pillar")
        pillar = (
            str(pillar_raw).strip().upper()
            if pillar_raw and str(pillar_raw).strip().upper() in {"R1", "R2", "R3", "R4"}
            else None
        )
        tags_raw = raw.get("tags") or []
        tags = (
            [str(t).strip() for t in tags_raw if str(t).strip()][:8]
            if isinstance(tags_raw, list)
            else []
        )
        items.append(
            {
                "type": "tezis",
                "title": title or body[:120],
                "body": body or title,
                "viral_score": score,
                "pillar": pillar,
                "tags": tags,
            }
        )
    return items
