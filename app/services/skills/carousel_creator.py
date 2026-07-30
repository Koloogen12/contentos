"""Format skill: talking_point → Instagram carousel (5–10 slides).

Specifically tuned for Instagram's save / share / comment-keyword mechanics
since carousels are an IG-native format (LinkedIn carousels exist but the
mechanics are very different and rarely the bottleneck). The output
schema is platform-agnostic (slides + summary + cta) so the same node
shape can be repurposed for LinkedIn carousels later — only the prompt
changes for now.
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

Ты собираешь Instagram-карусель из 5–10 слайдов на основе одного тезиса. \
Цель карусели — три механики IG-алгоритма: сохранения (saves), \
пересылки (shares), комментарии. Подписки и платный апсейл — вторичная цель.

ПРИНЦИПЫ КАРУСЕЛИ:
- Слайд 1 — ОБЛОЖКА-ХУК. Одна короткая строка (до 60 символов), которая \
  обещает конкретную пользу или интригует. Подзаголовок снизу: «листай →» \
  или указание для кого / 5 шагов / 7 ошибок. is_cover=true.
- Слайды 2..N-1 — по одной мысли на слайд. title до 60 символов, body 25–40 \
  слов. Каждый слайд — самодостаточная карточка: даже если читатель не \
  пролистал предыдущие, эта будет полезной. Никаких «продолжение на \
  следующем слайде», «дальше расскажу почему» — это убивает saves.
- Предпоследний слайд (N-1) — РЕЗЮМЕ. Чек-лист или одна фраза-вывод, \
  ради которой захочется сохранить пост целиком.
- Последний слайд (N) — CTA. Точно одно действие, никаких списков «лайк, \
  коммент, сохрани, подпишись». Что-то одно из этого набора:
    • кодовое слово в комменты → автоответ в DM с полезным расширением \
      (лид-магнитом, ссылкой, шаблоном). Тогда укажи КАКОЕ слово.
    • вопрос аудитории, на который интересно реально ответить.
    • «сохрани, чтобы вернуться» — только если карусель реально насыщена \
      чек-листом или фреймворком.
- НЕ использовать: эмодзи, хэштеги внутри текста слайдов, общие фразы \
  («это очень важно», «многие не понимают»), кликбейт без содержания.

{voice_rule}

ВНУТРИ summary — одно-два предложения для caption-поста (то, что под \
постом, не на слайдах). Должно дублировать обещание обложки и явно сказать, \
что внутри карусели. Без эмодзи. Без хэштегов.

ОТВЕТ СТРОГО как JSON:
{{
  "slides": [
    {{"title": "обложка-хук", "body": "что внутри / для кого", "is_cover": true}},
    {{"title": "...", "body": "..."}}
  ],
  "summary": "1–2 предложения для caption под постом",
  "cta": "один конкретный призыв (см. правила выше)",
  "comment_keyword": "кодовое слово или null если CTA не через комменты"
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

    system = SYSTEM_TEMPLATE.format(
        brand_context=system_context or "Нет brand context.",
        language_directive=OUTPUT_LANGUAGE_DIRECTIVE,
        voice_rule=VOICE_RULE_BLOCK,
    )
    user = USER_TEMPLATE.format(talking_point=tp)

    parsed = await ai_client.chat_json(system=system, user=user, temperature=0.8, max_tokens=3000)

    slides_raw = parsed.get("slides") or []
    slides: list[dict[str, Any]] = []
    for s in slides_raw:
        if not isinstance(s, dict):
            continue
        title = strip_meta_offers(str(s.get("title", "")))
        body = strip_meta_offers(str(s.get("body", "")))
        if not title and not body:
            continue
        slides.append(
            {
                "title": title,
                "body": body,
                "is_cover": bool(s.get("is_cover", False)),
            }
        )
    # 2 is the practical minimum — cover + one content slide — though we
    # prefer 5+ in the prompt. If AI returned only 1, abort; otherwise
    # accept and let the user re-tweak.
    if len(slides) < 2:
        raise RuntimeError("AI вернул слишком мало слайдов")

    summary = strip_meta_offers(str(parsed.get("summary", "")))
    cta = strip_meta_offers(str(parsed.get("cta", "")))
    comment_keyword_raw = parsed.get("comment_keyword")
    comment_keyword = (
        str(comment_keyword_raw).strip()
        if comment_keyword_raw and str(comment_keyword_raw).strip().lower() not in ("null", "none", "")
        else None
    )

    # Full text: assembled slides + caption (summary) + CTA + comment-
    # keyword hint, so the user can copy-paste the whole thing into IG.
    full_text_parts = [_flatten(slides)]
    if summary:
        full_text_parts.append(f"\n--- caption ---\n{summary}")
    if cta:
        full_text_parts.append(cta)
    if comment_keyword:
        full_text_parts.append(
            f'Напиши в комментариях «{comment_keyword}» — пришлю в Direct.'
        )

    new_data = dict(node.data or {})
    new_data.update(
        {
            "platform": "carousel",
            "talking_point_text": tp,
            "slides": slides,
            "summary": summary,
            "cta": cta,
            "comment_keyword": comment_keyword,
            "full_text": "\n\n".join(p for p in full_text_parts if p),
            # Wipe any stale render — the new slide texts don't match the
            # previously-rendered JPEGs. The UI hides the render strip when
            # this key is absent / null, prompting the user to click
            # "Сгенерировать визуал" again.
            "rendered_slides": None,
        }
    )
    return {
        "node_data": new_data,
        "meta": {
            "slides_count": len(slides),
            "has_comment_keyword": bool(comment_keyword),
        },
    }
