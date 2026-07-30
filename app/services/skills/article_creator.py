"""Format skill: talking_point → full blog article (markdown).

Long-form output (1000–3000 words). Structure:
  hook → intro → 3–5 sections with H2 → conclusion → CTA.
"""
from __future__ import annotations

import re
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

Ты пишешь развёрнутую статью для блога на основе одного тезиса. Цель — \
1500–2500 слов. Структура:

1. Заголовок (title) — короткий, до 70 символов, без кликбейта.
2. Hook — первая строка, до 110 символов.
3. Intro — 1–2 абзаца после хука, обозначь конкретный pain или вопрос.
4. Sections — 3–5 секций. У каждой:
   - heading (H2-уровня), до 70 символов
   - body — 200–400 слов, конкретика, пример или цифра
5. Conclusion — 1–2 абзаца, синтез, без воды.
6. CTA — одно предложение, конкретный action для читателя.
7. Meta description — 140–160 символов для SEO.

ФАКТИЧНОСТЬ:
- Цифры, имена, даты и примеры бери только из тезиса и brand context. \
  Не придумывай статистику, исследования и кейсы «как обычно бывает» — \
  выдуманная цифра рушит доверие ко всему тексту.
- Если для секции не хватает фактуры, лучше напиши её короче или разверни \
  через личный опыт автора, чем добери правдоподобной выдумкой.
- Оценки и обобщения («многие считают», «все больше компаний») — только \
  если в исходнике есть, на чём их основать.

Стиль: голос автора (см. brand context), без эмодзи, без хэштегов, без \
markdown-списков с буллетами для главных идей (используй абзацы), без \
пустого «в этой статье я расскажу». Сразу к сути.

{voice_rule}

ОТВЕТ СТРОГО как JSON:
{{
  "title": "...",
  "hook": "...",
  "intro": "...",
  "sections": [
    {{"heading": "...", "body": "..."}}
  ],
  "conclusion": "...",
  "cta": "...",
  "meta_description": "..."
}}"""

USER_TEMPLATE = """\
ТЕЗИС:
{talking_point}

Напиши статью."""


def _slugify(title: str) -> str:
    s = title.lower().strip()
    # transliterate Russian → latin (simple, lossy)
    table = str.maketrans({
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
        "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    })
    s = s.translate(table)
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s[:80] or "article"


def _assemble_markdown(parsed: dict) -> str:
    lines: list[str] = []
    if title := parsed.get("title"):
        lines.append(f"# {title}\n")
    if hook := parsed.get("hook"):
        lines.append(f"_{hook}_\n")
    if intro := parsed.get("intro"):
        lines.append(intro + "\n")
    for s in parsed.get("sections", []):
        if isinstance(s, dict):
            heading = s.get("heading", "").strip()
            body = s.get("body", "").strip()
            if heading:
                lines.append(f"\n## {heading}\n")
            if body:
                lines.append(body + "\n")
    if conclusion := parsed.get("conclusion"):
        lines.append(f"\n## Итог\n\n{conclusion}\n")
    if cta := parsed.get("cta"):
        lines.append(f"\n{cta}\n")
    return "\n".join(lines).strip()


@register("article_creator")
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

    # Long-form structured output — route to the "structured" model
    # (gpt-5.4 by default). Grok tends to under-fill 1500–2500 word
    # articles and miss the strict article-schema fields.
    from app.config import settings as _settings
    parsed = await ai_client.chat_json(
        system=system,
        user=user,
        temperature=0.7,
        max_tokens=6000,
        model=_settings.COMETAPI_MODEL_STRUCTURED,
    )

    title = strip_meta_offers(str(parsed.get("title", "")))
    if not title:
        raise RuntimeError("AI не вернул title")
    sections_raw = parsed.get("sections") or []
    sections: list[dict[str, str]] = []
    for s in sections_raw:
        if not isinstance(s, dict):
            continue
        heading = strip_meta_offers(str(s.get("heading", "")))
        body = strip_meta_offers(str(s.get("body", "")))
        if heading or body:
            sections.append({"heading": heading, "body": body})
    if len(sections) < 2:
        raise RuntimeError("AI вернул слишком мало секций")

    intro = strip_meta_offers(str(parsed.get("intro", "")))
    hook = strip_meta_offers(str(parsed.get("hook", "")))
    conclusion = strip_meta_offers(str(parsed.get("conclusion", "")))
    cta = strip_meta_offers(str(parsed.get("cta", "")))
    meta_description = strip_meta_offers(str(parsed.get("meta_description", "")))[:160]

    full_text = _assemble_markdown(
        {
            "title": title,
            "hook": hook,
            "intro": intro,
            "sections": sections,
            "conclusion": conclusion,
            "cta": cta,
        }
    )
    word_count = len(re.findall(r"\b[\w-]+\b", full_text, flags=re.UNICODE))

    new_data = dict(node.data or {})
    new_data.update(
        {
            "platform": "article",
            "talking_point_text": tp,
            "title": title,
            "slug": _slugify(title),
            "hook": hook,
            "intro": intro,
            "sections": sections,
            "conclusion": conclusion,
            "cta": cta,
            "meta_description": meta_description,
            "full_text": full_text,
            "word_count": word_count,
        }
    )
    return {"node_data": new_data, "meta": {"sections": len(sections), "words": word_count}}
