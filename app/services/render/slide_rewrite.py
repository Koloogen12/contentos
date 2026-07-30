"""AI helper: rewrite ONE carousel slide's title+body per user prompt.

Used by the `slide_tweak` worker task to support Virale-style per-slide
conversational edits ("сократи", "сделай ярче", "перепиши с акцентом
на скорость"). NOT a registered Skill — invoked internally from the
worker, not via the user-facing /run dispatch.

Why a separate module:
  - Keeps the prompt focused (only deals with single-slide rewrites)
  - Doesn't touch the existing carousel_creator's whole-deck logic
  - Easier to A/B different micro-prompts without risking the full-deck
    flow
"""
from __future__ import annotations

from app.services import ai_client
from app.services.skills.base import (
    OUTPUT_LANGUAGE_DIRECTIVE,
    VOICE_RULE_BLOCK,
    strip_meta_offers,
)


_SYSTEM_TEMPLATE = """\
{brand_context}

{language_directive}

Ты переписываешь ОДИН слайд карусели в Instagram по указанию автора. \
Меняй только этот слайд — остальные слайды и общая идея карусели тебе \
неизвестны и не должны влиять на твой ответ.

ПРАВИЛА СЛАЙДА:
- title до 60 символов, body 25–40 слов
- слайд должен быть самодостаточным (читатель мог не пролистать предыдущие)
- никаких «дальше расскажу» / «продолжение на следующем» — это убивает saves
- никаких эмодзи / хэштегов / шаблонных фраз
- сохрани смысл и тон, который был в исходном слайде, если автор не \
  попросил явно «изменить угол» / «переписать с нуля»

{voice_rule}

ОТВЕТ СТРОГО как JSON:
{{
  "title": "новый title",
  "body": "новый body"
}}"""


_USER_TEMPLATE = """\
ИСХОДНЫЙ СЛАЙД ({slide_position}):
title: {current_title}
body: {current_body}

УКАЗАНИЕ АВТОРА:
{user_prompt}

Перепиши слайд."""


async def rewrite_slide(
    *,
    system_context: str,
    current_title: str,
    current_body: str,
    user_prompt: str,
    slide_position: str = "обычный слайд",
) -> tuple[str, str]:
    """Rewrite a single slide's title and body per `user_prompt`.

    `slide_position` is a human-readable hint passed to the prompt
    ("слайд 3 из 7", "предпоследний слайд (РЕЗЮМЕ)", "финальный CTA") so
    the AI keeps role-appropriate framing. Caller is responsible for
    composing this hint based on `is_cover` / index / total.

    Returns `(title, body)` after `strip_meta_offers` filtering. Both
    strings may be empty if the AI returns nothing usable — caller
    should treat that as a tweak-failed scenario.
    """
    user_prompt = (user_prompt or "").strip()
    if not user_prompt:
        raise ValueError("Не указано, что менять в слайде")

    system = _SYSTEM_TEMPLATE.format(
        brand_context=system_context or "Нет brand context.",
        language_directive=OUTPUT_LANGUAGE_DIRECTIVE,
        voice_rule=VOICE_RULE_BLOCK,
    )
    user = _USER_TEMPLATE.format(
        slide_position=slide_position,
        current_title=current_title or "(пусто)",
        current_body=current_body or "(пусто)",
        user_prompt=user_prompt,
    )

    parsed = await ai_client.chat_json(
        system=system,
        user=user,
        temperature=0.75,
        max_tokens=800,
    )

    title = strip_meta_offers(str(parsed.get("title") or "").strip())
    body = strip_meta_offers(str(parsed.get("body") or "").strip())
    return title, body
