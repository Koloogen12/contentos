"""Generate an image prompt for the carousel cover from the slide content.

Not a registered skill — this is an internal call inside the render
pipeline, invisible to the user. It's a separate function so we can iterate
on the prompt-writing logic without touching the rendering loop.

The goal of the prompt is to produce a cover image that:
  1. Communicates the emotional tension of the talking point.
  2. Uses editorial / cinematic visual language (dark, dramatic lighting,
     symbolic objects) rather than literal stock-photo illustrations.
  3. Has enough dark area in the lower half so the white title overlay
     reads well after the gradient scrim is applied.

We always append a fixed style suffix to keep visual consistency across
carousels — same way Virale's covers all look like they came from one
art director. Founders can later override the style via Brand Visual
settings (Phase 2).
"""
from __future__ import annotations

import re

from app.services import ai_client


_STYLE_SUFFIX = (
    "Style: cinematic editorial photography, dramatic chiaroscuro lighting, "
    "high contrast, dark moody background with deep blacks and warm orange "
    "or red accents, fine grain, photorealistic, symbolic and metaphorical "
    "composition, generous negative space in the lower third for text "
    "overlay, no text, no logos, no watermarks, no faces, no readable letters. "
    "16-bit colour depth, shot on Hasselblad medium format."
)


_SYSTEM = """\
Ты — арт-директор. Тебе дают тезис карусели для соцсетей и заголовок \
обложки. Ты пишешь короткий image-prompt (на английском, до 70 слов) \
для генерации виральной обложки в стиле editorial photography.

Правила:
- Без текста, без логотипов, без лиц, без читаемых букв.
- Только метафора: символический объект или сцена, передающая эмоцию.
- Описать ОДИН центральный объект + lighting + mood + colour palette.
- Никакого стокового стиля (handshake, lightbulb, business person at desk).
- Темная палитра. Контраст. Тёплые акценты (огонь, лава, неон) допустимы.
- НЕ пиши Style: — стилевой суффикс добавится автоматически.

Верни строго JSON:
{"image_prompt": "..."}
"""


_USER_TEMPLATE = """\
Тезис карусели: {talking_point}

Заголовок обложки: {cover_title}

Напиши image_prompt."""


async def write_cover_prompt(*, talking_point: str, cover_title: str) -> str:
    """Generate an English image prompt + style suffix for the cover."""
    talking_point = (talking_point or "").strip()
    cover_title = (cover_title or "").strip()
    if not talking_point and not cover_title:
        # Defensive fallback: still return a usable prompt so the render
        # doesn't fail because the upstream tezis was empty.
        return (
            "A dark, atmospheric still life of a cracked obsidian sphere "
            "with glowing molten cracks. " + _STYLE_SUFFIX
        )

    parsed = await ai_client.chat_json(
        system=_SYSTEM,
        user=_USER_TEMPLATE.format(
            talking_point=talking_point or "(пусто)",
            cover_title=cover_title or "(пусто)",
        ),
        temperature=0.8,
        max_tokens=400,
    )
    base = str(parsed.get("image_prompt", "")).strip()
    if not base:
        # If the AI returns an empty payload, fall back to the title verbatim
        # rather than crashing the render. The style suffix alone still
        # produces an acceptable abstract image.
        base = f"Editorial photograph evoking the concept: {cover_title}."

    # Trim any stray quotes / markdown the model occasionally adds.
    base = re.sub(r'^["\'\s]+|["\'\s]+$', "", base)
    return f"{base}\n\n{_STYLE_SUFFIX}"
