"""AI image generation for carousel covers (and later, full AI-carousels).

Wraps the CometAPI image endpoint (OpenAI-compatible `/images/generations`)
that proxies `gpt-image-1`. Returns raw PNG bytes — caller is responsible
for any resizing / format conversion (Pillow lives in playwright_runner).

The function is intentionally narrow: one prompt → one image. Higher-level
prompt construction (turning a carousel `talking_point` into a usable image
prompt) lives in `app/services/skills/cover_prompt_writer.py` so it can
benefit from the brand-context system prompt like every other skill.

Cost / latency notes (gpt-image-1 standard quality, 1024×1536):
- ~$0.04 per image
- ~8–15 seconds end-to-end
- Returns base64-encoded PNG in `data[0].b64_json` — we decode locally so
  the URL never round-trips through OpenAI's hosted images.
"""
from __future__ import annotations

import base64
from typing import Literal

from openai import AsyncOpenAI

from app.config import settings


def _client() -> AsyncOpenAI:
    """Build the OpenAI client pointing at CometAPI."""
    if not settings.COMETAPI_KEY:
        raise RuntimeError(
            "COMETAPI_KEY is empty — image generation unavailable. "
            "Set it in .env or skip cover rendering."
        )
    return AsyncOpenAI(
        api_key=settings.COMETAPI_KEY,
        base_url=settings.COMETAPI_BASE_URL,
    )


async def generate_cover_image(
    prompt: str,
    *,
    size: Literal["1024x1024", "1024x1536", "1536x1024"] = "1024x1536",
    style: Literal["vivid", "natural"] = "vivid",
) -> bytes:
    """Generate a single cover image, return raw PNG bytes.

    `prompt` should already include style instructions — gpt-image-1
    follows literal prompts well, so passing "photorealistic, dark mood,
    cracked rocket with lava cracks, editorial style" works as expected.

    `size=1024x1536` matches our 4:5 cover aspect ratio with minimal
    cropping when fitted into the 1080×1350 slide canvas.
    """
    if not prompt.strip():
        raise ValueError("Empty image prompt")

    response = await _client().images.generate(
        model=settings.COMETAPI_MODEL_IMAGE,
        prompt=prompt,
        size=size,
        n=1,
        # No `response_format=...` — gpt-image-1 always returns b64_json
        # and rejects the parameter with HTTP 400 "Unknown parameter:
        # 'response_format'". The flag was a dall-e-3 thing that didn't
        # carry over to the new model. We decode the b64 unconditionally
        # below — the field is always present in the response shape.
    )
    data = response.data
    if not data:
        raise RuntimeError("Image API returned no data")
    b64 = data[0].b64_json
    if not b64:
        raise RuntimeError("Image API returned empty b64_json")
    return base64.b64decode(b64)


def to_data_url(png_bytes: bytes, mime: str = "image/png") -> str:
    """Encode PNG bytes as a data: URL so we can embed in HTML without
    a network fetch. Used by the cover template to set the background
    image inline before Playwright takes the screenshot.
    """
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return f"data:{mime};base64,{encoded}"
