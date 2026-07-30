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
5. key_points — блок «Главное», 3–5 пунктов. Это суждение О МАТЕРИАЛЕ \
   целиком: насколько он глубок, что охватывает, кому и зачем идти в \
   первоисточник. НЕ пересказ выводов из разделов ниже — если пункт можно \
   переставить в раздел и там он будет уместен, значит он написан не о том. \
   Читатель не должен встречать одну мысль дважды.
6. sections — разбор по темам. 3–7 разделов, у каждого:
   - heading — название темы, до 60 символов
   - body — СВЯЗНЫЙ ТЕКСТ, 2–5 абзацев. Не список, не буллеты, не набор \
     обрубленных фраз. Абзацы связаны между собой: мысль вытекает из \
     предыдущей, а не начинается заново.
7. afterword — послесловие, 2–4 предложения: что автор забрал себе, с чем \
   спорит, что применит.

ГЛАВНОЕ ПРАВИЛО ЖАНРА: это рецензия, а не конспект. Разница в том, кто \
говорит. Конспект пересказывает материал — рецензию пишет человек, который \
его посмотрел и имеет о нём мнение. Читатель должен слышать автора на \
протяжении всего текста, а не только в оценке и послесловии.

Как это выглядит в разделе: сначала что говорит материал, потом — что об \
этом думает автор. «Он утверждает X. По моему опыту это работает только \
когда Y» — вот нормальная пара предложений. Голая пересказанная мысль без \
реакции автора — брак.

ЖЁСТКИЕ ПРАВИЛА ФАКТИЧНОСТИ (они не про мнение, а про факты):
- Цифры, имена, цитаты и примеры берутся ТОЛЬКО из материала. Ничего не \
  додумывай и не достраивай «как обычно бывает».
- Если в материале чего-то нет — не пиши об этом. Лучше короче, но честно.
- Не приписывай автору материала того, чего он не говорил. Когда споришь, \
  ясно разделяй: это сказал он, а это думаю я.
- Своё мнение можно и нужно везде. Ограничение касается фактов, а не \
  суждений: выдумывать чужие цифры нельзя, иметь свою позицию — обязательно.

Стиль: голос автора из brand context и его образцов, первое лицо. Без \
эмодзи, без хэштегов, без «в этой статье я расскажу». Сразу к делу.

ЧЕГО НЕ ДЕЛАТЬ СО СЛОГОМ (иначе текст выдаёт машину):
- Противопоставления вида «это не X, это Y» и «не X, а Y» — не больше \
  двух на всю рецензию, и никогда не повторяй одну и ту же пару дважды. \
  «Это не слабость, это честность» и следом «это не слабость, это зрелость» \
  — брак. Вместо противопоставления ставь прямое утверждение с конкретикой.
- Не начинай абзацы одинаково. Три абзаца подряд с «Вот это…» или «И тут…» \
  читаются как шаблон, даже если каждый по отдельности хорош.
- Обращение к читателю держи одно на весь текст. Начал на «ты» — не \
  переходи на «вы» в финале.

{voice_rule}

ОТВЕТ СТРОГО как JSON:
{{
  "title": "...",
  "subtitle": "...",
  "verdict": "...",
  "audience": "...",
  "key_points": ["...", "..."],
  "sections": [
    {{"heading": "...", "body": "связный текст в 2-5 абзацев"}}
  ],
  "afterword": "..."
}}"""

# Напоминание про язык стоит ПОСЛЕ материала намеренно. В системном
# промпте оно тоже есть, но между ним и генерацией лежат десятки тысяч
# знаков англоязычного транскрипта, и модель начинает зеркалить язык
# источника: «был бы exercise in vanity», «поэтому look on the bright
# side». Инструкция сразу перед ответом такого не допускает.
USER_TEMPLATE = """\
МАТЕРИАЛ (полный текст источника):
{source_content}

ТЕЗИСЫ, КОТОРЫЕ УЖЕ ИЗВЛЕЧЕНЫ ИЗ МАТЕРИАЛА:
{talking_points}

Напиши рецензию на весь материал целиком.

Материал может быть на английском — рецензия пишется по-русски целиком. \
Английские обороты в русской фразе недопустимы: не «был бы exercise in \
vanity», а «был бы упражнением в тщеславии». Оставляй как есть только \
имена, названия продуктов и термины без русского эквивалента."""

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
        for s in p["sections"]:
            heading = (s.get("heading") or "").strip()
            if heading:
                lines.append(f"\n## {heading}\n")
            # Разделы — связный текст, поэтому заголовок «Тезисы» над ними
            # больше не нужен: он превращал рецензию в конспект уже на
            # уровне оглавления.
            body = (s.get("body") or "").strip()
            if body:
                lines.append(body + "\n")
            else:
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
        # Раздел стал связным текстом вместо списка пунктов, и объём ответа
        # вырос в разы: шесть разделов по 2–5 абзацев в 8000 токенов уже не
        # помещаются, ответ обрывался на середине и падал как «невалидный
        # JSON». Диагноз был неочевидным — ошибка указывала на разбор, а
        # причина была в лимите.
        max_tokens=16000,
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
        body = strip_meta_offers(str(s.get("body", ""))).strip()
        # Модель иногда всё равно сваливается в список — тогда склеиваем
        # пункты в абзацы, а не показываем пользователю пустой раздел.
        if not body:
            body = "\n\n".join(clean_list(s.get("points")))
        if heading or body:
            sections.append({"heading": heading, "body": body})
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
