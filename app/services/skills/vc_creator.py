"""Format skill: talking_point → материал для vc.ru.

Отличие от article_creator: у vc.ru есть собственные основания, по которым
блог получает коммерческий статус, и они содержательные, а не косметические.
Материал в этом статусе скрыт из лент и не индексируется, больше одного
в месяц публиковать нельзя. Поэтому промпт держит не «стиль площадки», а
конкретные требования модерации.

Главные из них (skills/vc-ru-blog):
  * активные ссылки на свои сайты и соцсети трактуются как извлечение
    коммерческой выгоды — на практике строже, чем написано в публичных
    правилах. Упоминать продукт текстом можно, ставить гиперссылку нельзя;
  * материал должен оставаться полезным, если вырезать все упоминания
    продукта. Не остаётся — это продвижение, и переписывание слов не спасёт;
  * «глубина проработки» — прямое основание: нужны конкретные числа
    с контекстом, а не общие рассуждения;
  * материал без описанной ошибки или потери читается как реклама;
  * заголовок прямой, не фигуральный — прямое требование редакции;
  * финал не может быть предложением или моралью.

Skill не выдумывает фактуру: чего нет в исходном тезисе и brand context —
идёт в `missing_facts` вопросом автору. На vc выдуманная цифра стоит дороже,
чем где-либо: комментаторы проверяют.
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

Ты пишешь материал для vc.ru. Площадка присваивает блогу коммерческий \
статус по содержательным основаниям, и тогда материалы скрываются из лент \
и не индексируются. Поэтому правила ниже — не стиль, а условия публикации.

ГЛАВНЫЙ ТЕСТ: останется ли материал полезным читателю, если вырезать из \
него все упоминания продукта автора? Не остаётся — переписывай, смягчение \
формулировок не поможет.

ЗАПРЕЩЕНО:
- Любые призывы к действию: «попробуйте», «оставьте заявку», «пишите», \
  промокоды, цены, приглашения. Ни одного.
- Ссылки и приглашения в финале. Финал, из которого хочется дать ссылку, \
  означает, что материал писался ради ссылки.
- Фигуральный заголовок. Редакция прямо требует прямых заголовков, \
  передающих суть.
- Продукт как главный герой. Он может быть только инструментом внутри \
  чьей-то истории.
- Все цифры в свою пользу и ни одного провала — так читается реклама.

ОБЯЗАТЕЛЬНО:
- Не меньше трёх конкретных чисел с контекстом: сроки, суммы, объёмы, \
  сколько заняло, сколько стоило. Общие рассуждения без чисел площадка \
  считает низкой глубиной проработки.
- Хотя бы одна описанная ошибка, потеря или тупик. Честно, с цифрой.
- О своём продукте — только в прошедшем времени и как материал кейса: \
  что делали и что получилось, а не что продукт умеет.
- Связь с продуктом называть прямо в тексте, если она есть. Скрытая \
  аффилиация — то, за что снимают материал.
- Структура: H2 — разделы, H3 — подразделы. Врезка не длиннее двух \
  коротких предложений.

ФАКТИЧНОСТЬ: цифры, кейсы и цитаты берутся только из тезиса и brand \
context. Ничего не выдумывай — на vc комментаторы проверяют. Всё, чего \
не хватает, выпиши в missing_facts вопросом автору.

ФИНАЛ: не предложение и не мораль. Следующий шаг для читателя, открытый \
вопрос или честное завершение истории.

{voice_rule}

ОТВЕТ СТРОГО как JSON:
{{
  "title": "прямой заголовок, до 90 символов",
  "subtitle": "одна-две строки обычным текстом",
  "sections": [
    {{"heading": "H2", "body": "текст раздела"}}
  ],
  "ending": "финал без призыва",
  "self_check": {{
    "numbers_used": ["число с контекстом", "..."],
    "failure_described": "какая ошибка или потеря описана",
    "survives_product_cut": true,
    "risks": ["что в тексте может вызвать вопросы модерации, если есть"]
  }},
  "missing_facts": ["чего не хватило и надо добыть автору"]
}}"""

USER_TEMPLATE = """\
ТЕЗИС:
{talking_point}

Напиши материал для vc.ru."""


def _assemble_markdown(p: dict[str, Any]) -> str:
    lines: list[str] = []
    if title := p.get("title"):
        lines.append(f"# {title}\n")
    if subtitle := p.get("subtitle"):
        lines.append(subtitle + "\n")
    for s in p.get("sections", []):
        heading = (s.get("heading") or "").strip()
        body = (s.get("body") or "").strip()
        if heading:
            lines.append(f"\n## {heading}\n")
        if body:
            lines.append(body + "\n")
    if ending := p.get("ending"):
        lines.append("\n" + ending + "\n")
    return "\n".join(lines).strip()


# Кликабельная ссылка в теле — самая механическая причина коммерческого
# статуса, и проверяется она за секунду. Ловим и markdown-ссылки, и голые URL.
_LINK_PATTERNS = [
    re.compile(r"\[[^\]]+\]\(https?://[^)]+\)"),
    re.compile(r"https?://\S+"),
    re.compile(r"\b(?:t\.me|vk\.com|instagram\.com)/\S+", re.IGNORECASE),
]


def find_links(text: str) -> list[str]:
    found: list[str] = []
    for pat in _LINK_PATTERNS:
        found.extend(pat.findall(text))
    return sorted(set(found))


@register("vc_creator")
async def run(
    db: AsyncSession,
    node: Node,
    system_context: str,
    skill_input: dict[str, Any],
) -> dict[str, Any]:
    tp = (skill_input.get("talking_point") or "").strip()
    if not tp:
        raise ValueError("Нет входного тезиса")

    from app.config import settings as _settings

    parsed = await ai_client.chat_json(
        system=SYSTEM_TEMPLATE.format(
            brand_context=system_context or "Нет brand context.",
            language_directive=OUTPUT_LANGUAGE_DIRECTIVE,
            voice_rule=VOICE_RULE_BLOCK,
        ),
        user=USER_TEMPLATE.format(talking_point=tp),
        temperature=0.7,
        max_tokens=8000,
        model=_settings.COMETAPI_MODEL_STRUCTURED,
    )

    title = strip_meta_offers(str(parsed.get("title", "")))
    if not title:
        raise RuntimeError("AI не вернул заголовок")

    sections: list[dict[str, str]] = []
    for s in parsed.get("sections") or []:
        if not isinstance(s, dict):
            continue
        heading = strip_meta_offers(str(s.get("heading", ""))).strip()
        body = strip_meta_offers(str(s.get("body", ""))).strip()
        if heading or body:
            sections.append({"heading": heading, "body": body})
    if len(sections) < 2:
        raise RuntimeError("AI вернул слишком мало разделов")

    payload = {
        "title": title,
        "subtitle": strip_meta_offers(str(parsed.get("subtitle", ""))),
        "sections": sections,
        "ending": strip_meta_offers(str(parsed.get("ending", ""))),
    }
    full_text = _assemble_markdown(payload)

    raw_check = parsed.get("self_check") or {}
    self_check = {
        "numbers_used": [str(x) for x in (raw_check.get("numbers_used") or [])],
        "failure_described": str(raw_check.get("failure_described") or ""),
        "survives_product_cut": bool(raw_check.get("survives_product_cut")),
        "risks": [str(x) for x in (raw_check.get("risks") or [])],
        # Проверяем сами, а не верим модели на слово: ссылки — самое
        # механическое основание для статуса, и его легко проверить кодом.
        "active_links": find_links(full_text),
    }

    new_data = dict(node.data or {})
    new_data.update(
        {
            "platform": "vc",
            "talking_point_text": tp,
            **payload,
            "full_text": full_text,
            "word_count": len(re.findall(r"\b[\w-]+\b", full_text, flags=re.UNICODE)),
            "self_check": self_check,
            "missing_facts": [str(x) for x in (parsed.get("missing_facts") or [])],
        }
    )
    return {
        "node_data": new_data,
        "meta": {
            "sections": len(sections),
            "numbers": len(self_check["numbers_used"]),
            "active_links": len(self_check["active_links"]),
            "survives_product_cut": self_check["survives_product_cut"],
        },
    }
