"""Extract skill (mode=summary): source content → structured summary.

Alternative to `viral_talking_points`. Same input (upstream source's
content), different output shape:

  - summary: a 200–400 word distilled retelling of the source
  - key_points: 5–10 one-line bullets (the gist, not viral candidates)
  - actionable_takeaways: 3–5 concrete actions the reader can do
  - context_line: a 1-sentence "what is this material about" line

Used when the founder wants the *gist* of a transcript / article, not a
set of viral hooks. Downstream format nodes treat `summary` as the
talking-point body (see brand_context.collect_input_for_skill).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Node
from app.services import ai_client
from app.services.skills.base import OUTPUT_LANGUAGE_DIRECTIVE, register

SYSTEM_TEMPLATE = """\
{brand_context}

{language_directive}

Ты — редактор-аналитик. Твоя задача — сжать исходный материал до удобной \
сводки, которую можно использовать как опору для постов или просто \
держать как заметку «о чём вообще это было».

ЧТО ВЕРНУТЬ:
1. summary — 200–400 слов своими словами. НЕ пересказ "автор сказал X, \
   потом Y", а связный текст: что обсуждается, к каким выводам приходит, \
   какие конкретные примеры/цифры/имена названы. Сохраняй имена, цифры, \
   названия как в оригинале.
2. key_points — 5–10 коротких bullet-пунктов (по одной строке, без точки в конце), \
   каждый — самостоятельная мысль из материала. Это НЕ виральные тезисы и НЕ хуки, \
   это именно сжатие в формате "что было сказано".
3. actionable_takeaways — 3–5 конкретных действий или применений, которые \
   читатель может вынести лично для себя. Формулировка глаголом в инфинитиве \
   («использовать X», «начать с Y», «избегать Z»).
4. context_line — одно предложение: что это за материал и для кого он полезен.

НЕ использовать: эмодзи, восклицательные знаки, корпоративные слова \
(«синергия», «инновационное решение»), кликбейт.

ОТВЕТ СТРОГО как JSON:
{{
  "summary": "200–400 слов",
  "key_points": ["...", "...", "..."],
  "actionable_takeaways": ["...", "...", "..."],
  "context_line": "одно предложение"
}}"""

USER_TEMPLATE = """\
ИСХОДНЫЙ МАТЕРИАЛ (до 8000 символов, может быть транскрипт):

{content}"""


@register("content_summarizer")
async def run(
    db: AsyncSession,
    node: Node,
    system_context: str,
    skill_input: dict[str, Any],
) -> dict[str, Any]:
    content = skill_input.get("source_content") or ""
    if not content.strip():
        raise ValueError("Источник пустой")

    system = SYSTEM_TEMPLATE.format(
        brand_context=system_context or "Нет brand context.",
        language_directive=OUTPUT_LANGUAGE_DIRECTIVE,
    )
    user = USER_TEMPLATE.format(content=content[:8000])

    # Summary needs fidelity (≈ accurate retelling) more than voice
    # punch — use the structured model so we get reliable longer
    # paragraphs and tight key-points/takeaways arrays.
    from app.config import settings as _settings
    parsed = await ai_client.chat_json(
        system=system,
        user=user,
        temperature=0.5,
        max_tokens=4000,
        model=_settings.COMETAPI_MODEL_STRUCTURED,
    )

    summary = str(parsed.get("summary", "")).strip()
    if not summary:
        raise RuntimeError("AI не вернул summary")

    key_points = [
        str(p).strip()
        for p in (parsed.get("key_points") or [])
        if str(p).strip()
    ]
    takeaways = [
        str(t).strip()
        for t in (parsed.get("actionable_takeaways") or [])
        if str(t).strip()
    ]
    context_line = str(parsed.get("context_line", "")).strip()

    new_data = dict(node.data or {})
    new_data.update(
        {
            "extract_mode": "summary",
            "summary": summary,
            "key_points": key_points,
            "actionable_takeaways": takeaways,
            "context_line": context_line,
            # Clear the talking-points payload to avoid showing stale data
            # in the UI when the user switches modes back and forth.
            "talking_points": [],
            "selected_index": None,
        }
    )

    return {
        "node_data": new_data,
        "meta": {
            "key_points_count": len(key_points),
            "takeaways_count": len(takeaways),
            "summary_chars": len(summary),
        },
    }
