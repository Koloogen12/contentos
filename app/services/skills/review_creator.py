"""Format skill: whole source + all theses → author's review of the material.

The gap this fills: every other format skill takes ONE talking point and
expands it into a post. There was no way to say "I watched this video / read
this book — write my review of the whole thing". The user would get twenty
separate posts about twenty theses and nothing that covers the material.

Shape follows the genre the founder already writes by hand: a verdict, a
short "Главное" block (why it's worth your time, what it covers), the theses
grouped under headings, and a personal afterword about what the author takes
away. Not a neutral summary — a review with an opinion, in first person.

Input differs from the other format skills: `source_content` (the full
transcript / article) plus `talking_points` (everything the extract node
found), not a single `talking_point`. See `brand_context.collect_input_for_skill`.
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

Ты пишешь РЕЦЕНЗИЮ автора на изученный материал: видео, книгу, статью, \
доклад. Это не пересказ и не саммари — это личный разбор от первого лица: \
что здесь ценного, кому это нужно, что ты забрал себе.

Жанр, на который равняемся: автор посмотрел материал, поставил оценку, \
выделил главное, выписал тезисы по темам и в конце сказал, что лично он \
из этого унёс.

Структура:

1. title — название материала так, как его назвал бы автор поста. \
   До 80 символов, без кликбейта.
2. subtitle — одна строка о том, что это за материал и про что он.
3. verdict — оценка вида «9/10» и одно предложение, почему именно такая. \
   Оценка честная: если материал слабый, так и пиши.
4. audience — одна строка: кому стоит смотреть/читать, а кому нет.
5. key_points — блок «Главное», 3–5 пунктов. Здесь общее суждение о \
   материале, а не его содержание: насколько он глубок, что охватывает, \
   стоит ли идти в первоисточник.
6. sections — тезисы, СГРУППИРОВАННЫЕ по темам. 3–7 групп, у каждой:
   - heading — название темы, до 60 символов
   - points — 3–8 пунктов, каждый самостоятельная мысль из материала. \
     Формулируй плотно: утверждение, при необходимости пример или цифра \
     из материала.
7. afterword — послесловие от автора, 2–4 предложения: что он забрал \
   себе, с чем спорит, что применит. Здесь допустимо и нужно личное мнение.

ЖЁСТКИЕ ПРАВИЛА ФАКТИЧНОСТИ:
- Все тезисы, цифры, имена и примеры берутся ТОЛЬКО из материала. \
  Ничего не додумывай и не достраивай «как обычно бывает».
- Если в материале чего-то нет — не пиши об этом. Лучше короче, но честно.
- Личное мнение автора допустимо только в verdict, audience и afterword. \
  В sections — содержание материала, без домыслов.
- Не приписывай автору материала того, чего он не говорил.

Стиль: голос автора из brand context, первое лицо, без эмодзи, без \
хэштегов, без «в этой статье я расскажу». Сразу к делу.

{voice_rule}

ОТВЕТ СТРОГО как JSON:
{{
  "title": "...",
  "subtitle": "...",
  "verdict": "...",
  "audience": "...",
  "key_points": ["...", "..."],
  "sections": [
    {{"heading": "...", "points": ["...", "..."]}}
  ],
  "afterword": "..."
}}"""

USER_TEMPLATE = """\
МАТЕРИАЛ (полный текст источника):
{source_content}

ТЕЗИСЫ, КОТОРЫЕ УЖЕ ИЗВЛЕЧЕНЫ ИЗ МАТЕРИАЛА:
{talking_points}

Напиши рецензию на весь материал целиком."""

# Хвост транскрипта, который отдаём модели. Часовое видео — это ~50–60К
# символов; вместе с тезисами и brand context это уже близко к пределу
# окна, а качество разбора от последних абзацев почти не растёт.
MAX_SOURCE_CHARS = 40_000


def _assemble_markdown(p: dict[str, Any]) -> str:
    lines: list[str] = []
    if title := p.get("title"):
        lines.append(f"# {title}\n")
    if subtitle := p.get("subtitle"):
        lines.append(f"_{subtitle}_\n")

    meta = [x for x in (p.get("verdict"), p.get("audience")) if x]
    if meta:
        lines.append("\n".join(meta) + "\n")

    if key_points := p.get("key_points"):
        lines.append("\n## Главное\n")
        lines.extend(f"- {kp}" for kp in key_points)
        lines.append("")

    if p.get("sections"):
        lines.append("\n## Тезисы\n")
        for s in p["sections"]:
            heading = (s.get("heading") or "").strip()
            if heading:
                lines.append(f"\n### {heading}\n")
            lines.extend(f"- {pt}" for pt in s.get("points", []))
        lines.append("")

    if afterword := p.get("afterword"):
        lines.append(f"\n## Что я забрал себе\n\n{afterword}\n")
    return "\n".join(lines).strip()


@register("review_creator")
async def run(
    db: AsyncSession,
    node: Node,
    system_context: str,
    skill_input: dict[str, Any],
) -> dict[str, Any]:
    source = (skill_input.get("source_content") or "").strip()
    tps: list[dict[str, Any]] = skill_input.get("talking_points") or []

    if not source and not tps:
        raise ValueError(
            "Нечего рецензировать: подключи ноду с материалом или извлечением идей"
        )

    tp_lines = "\n".join(
        f"{i + 1}. {str(tp.get('text', '')).strip()}"
        for i, tp in enumerate(tps)
        if str(tp.get("text", "")).strip()
    ) or "(тезисы не извлекались — работай по тексту материала)"

    system = SYSTEM_TEMPLATE.format(
        brand_context=system_context or "Нет brand context.",
        language_directive=OUTPUT_LANGUAGE_DIRECTIVE,
        voice_rule=VOICE_RULE_BLOCK,
    )
    user = USER_TEMPLATE.format(
        source_content=source[:MAX_SOURCE_CHARS] or "(текст источника недоступен)",
        talking_points=tp_lines,
    )

    from app.config import settings as _settings

    parsed = await ai_client.chat_json(
        system=system,
        user=user,
        temperature=0.7,
        max_tokens=8000,
        model=_settings.COMETAPI_MODEL_STRUCTURED,
    )

    title = strip_meta_offers(str(parsed.get("title", "")))
    if not title:
        raise RuntimeError("AI не вернул название материала")

    def clean_list(raw: Any) -> list[str]:
        out: list[str] = []
        for item in raw or []:
            text = strip_meta_offers(str(item)).strip()
            if text:
                out.append(text)
        return out

    sections: list[dict[str, Any]] = []
    for s in parsed.get("sections") or []:
        if not isinstance(s, dict):
            continue
        heading = strip_meta_offers(str(s.get("heading", ""))).strip()
        points = clean_list(s.get("points"))
        if heading or points:
            sections.append({"heading": heading, "points": points})
    if not sections:
        raise RuntimeError("AI не разложил материал на темы")

    payload = {
        "title": title,
        "subtitle": strip_meta_offers(str(parsed.get("subtitle", ""))),
        "verdict": strip_meta_offers(str(parsed.get("verdict", ""))),
        "audience": strip_meta_offers(str(parsed.get("audience", ""))),
        "key_points": clean_list(parsed.get("key_points")),
        "sections": sections,
        "afterword": strip_meta_offers(str(parsed.get("afterword", ""))),
    }
    full_text = _assemble_markdown(payload)
    word_count = len(re.findall(r"\b[\w-]+\b", full_text, flags=re.UNICODE))

    new_data = dict(node.data or {})
    new_data.update(
        {
            "platform": "review",
            **payload,
            "full_text": full_text,
            "word_count": word_count,
            # Сколько тезисов легло в разбор — видно, что рецензия сделана
            # по всему материалу, а не по одному пункту.
            "source_tezis_count": len(tps),
        }
    )
    return {
        "node_data": new_data,
        "meta": {
            "sections": len(sections),
            "tezis_count": len(tps),
            "word_count": word_count,
            "source_chars": len(source),
        },
    }
