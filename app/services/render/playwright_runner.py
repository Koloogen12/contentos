"""Playwright wrapper: HTML string → JPEG bytes at 3x DPI.

We launch a single Chromium instance per `RenderSession`, then reuse the
same page for every slide. The runner is reset between renders so font
caches don't leak across canvases, but Playwright's internal browser
process can stay warm via Arq's worker lifecycle if we want to optimise
further down the line.

JPEG quality is hardcoded at 88 — visually lossless for typography-heavy
content while keeping each slide under ~300 KB. Pillow post-processing
strips EXIF / colour-profile metadata; without it Chromium screenshots
embed an sRGB ICC profile (~6 KB per slide) that some IG uploaders reject.
"""
from __future__ import annotations

import io
from contextlib import asynccontextmanager
from typing import AsyncIterator

from PIL import Image
from playwright.async_api import async_playwright

from app.services.render.templates import SLIDE_HEIGHT, SLIDE_WIDTH

# devicePixelRatio=3 gives 3240×4050 — same as Virale, downscales crisply.
DEVICE_SCALE_FACTOR = 3

# Hardcoded JPEG settings. Re-encoding via Pillow rather than letting
# Playwright write the JPEG directly because Playwright's JPEG path passes
# through Chromium's libjpeg which embeds the sRGB ICC profile we don't want.
JPEG_QUALITY = 88


class RenderSession:
    """Context for rendering N slides on a single browser instance.

    Usage::

        async with RenderSession.open() as session:
            slide_1_bytes = await session.render_html_to_jpeg(html_1)
            slide_2_bytes = await session.render_html_to_jpeg(html_2)
    """

    def __init__(self, page, browser, playwright):
        self._page = page
        self._browser = browser
        self._playwright = playwright

    @classmethod
    @asynccontextmanager
    async def open(cls) -> AsyncIterator["RenderSession"]:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=True,
            # --font-render-hinting=none lets fonts render at the same
            # weight Chromium uses for screenshots on macOS. Without this
            # flag the hinting differs from the user's preview which
            # shows the HTML preview in their browser.
            args=[
                "--font-render-hinting=none",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            viewport={"width": SLIDE_WIDTH, "height": SLIDE_HEIGHT},
            device_scale_factor=DEVICE_SCALE_FACTOR,
            # Locale matters: Manrope cyrillic glyphs render correctly
            # only when the document locale is ru-RU.
            locale="ru-RU",
        )
        page = await context.new_page()
        try:
            yield cls(page, browser, playwright)
        finally:
            await context.close()
            await browser.close()
            await playwright.stop()

    async def render_html_to_jpeg(self, html: str) -> bytes:
        """Render a single HTML document and return optimised JPEG bytes."""
        await self._page.set_content(
            html,
            # `networkidle` waits for the Google Fonts request to finish.
            # Without it the first slide screenshots with fallback fonts
            # (Helvetica) before Manrope downloads.
            wait_until="networkidle",
        )
        # Belt-and-braces: also wait for document.fonts.ready in case
        # networkidle fires before the @font-face declaration parses.
        await self._page.evaluate("document.fonts.ready")

        png_bytes = await self._page.screenshot(
            type="png",
            full_page=False,
            omit_background=False,
        )
        return _png_to_optimised_jpeg(png_bytes)


def _png_to_optimised_jpeg(png_bytes: bytes) -> bytes:
    """Re-encode a PNG screenshot as JPEG without ICC profiles.

    PIL's `save(..., format='JPEG', icc_profile=None, ...)` drops Chromium's
    embedded sRGB profile. We force RGB mode because Chromium may return
    RGBA for pages with transparent regions (cover slides during the
    AI-image generation moment briefly have transparent backgrounds).
    """
    with Image.open(io.BytesIO(png_bytes)) as img:
        rgb = img.convert("RGB")
        out = io.BytesIO()
        rgb.save(
            out,
            format="JPEG",
            quality=JPEG_QUALITY,
            optimize=True,
            progressive=True,
            subsampling=2,  # 4:2:0 — standard for web JPEGs
        )
        return out.getvalue()
