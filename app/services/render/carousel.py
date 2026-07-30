"""Carousel render orchestrator.

Public entrypoint: `render_carousel_for_node(node, brand_visual)`.

Reads `node.data["slides"]` (produced by `carousel_creator` skill) and:
  1. Generates the cover AI image (one HTTP call to gpt-image-1).
  2. Writes cover prompt → image via `cover_prompt.write_cover_prompt`.
  3. Renders all slides via a single Playwright session.
  4. Uploads each JPEG to S3 under `renders/carousel/{node_id}/{render_id}/`.
  5. Returns a `RenderResult` with the list of public URLs.

This function deliberately does NOT mutate `node.data` — the caller
(`/api/v1/nodes/{id}/render-visual` handler) is responsible for persisting
the result into `node.data["rendered_slides"]` after the worker completes.
Keeping mutation out of the renderer lets us unit-test it without a DB.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.services import images, storage
from app.services.render import cover_prompt, templates
from app.services.render.playwright_runner import RenderSession

log = logging.getLogger(__name__)


# Brand-visual struct that the orchestrator needs to render. Eventually
# this comes from `Project.brand_visual` (added in Phase 2). For now we
# default everything to the Virale-style editorial-dark look.
@dataclass
class BrandVisual:
    username: str | None = None
    show_numbering: bool = True
    prepend_slide_number: bool = True
    eyebrow_text: str = "ОШИБКА"  # generic; override per carousel via slide.eyebrow_hint
    # Style identifier — only "editorial_dark" supported in MVP. Reserved
    # for a future visual-style picker in the UI.
    style: str = "editorial_dark"


@dataclass
class SlideRender:
    index: int  # 1-based — matches how users see it
    is_cover: bool
    url: str
    width: int
    height: int


@dataclass
class RenderResult:
    render_id: str
    slides: list[SlideRender]
    cover_prompt: str
    duration_seconds: float
    style: str
    meta: dict[str, Any] = field(default_factory=dict)


async def render_carousel_for_node(
    *,
    node_id: str,
    slides_data: list[dict[str, Any]],
    talking_point: str,
    brand_visual: BrandVisual,
) -> RenderResult:
    """Render all slides for a carousel node.

    `slides_data` is the raw list from `node.data["slides"]` — each item is
    a dict with `title`, `body`, optional `is_cover`, optional `eyebrow_hint`.

    Raises `RuntimeError` if any individual slide fails — we don't partial-
    render because a missing slide N would leave a hole the user can't easily
    fix from the UI. Re-running is cheap (the cover is the expensive part).
    """
    if not slides_data:
        raise ValueError("Нет слайдов для рендера")

    render_id = uuid.uuid4().hex
    started = asyncio.get_event_loop().time()

    # Identify the cover slide. If the AI didn't mark one, treat the first
    # slide as the cover — matches how Instagram users perceive it.
    cover_idx = next(
        (i for i, s in enumerate(slides_data) if s.get("is_cover")),
        0,
    )
    cover = slides_data[cover_idx]
    total = len(slides_data)

    # Step 1: write the cover image prompt and generate the image. Done
    # before the Playwright session opens so we don't waste a warm
    # browser idling while gpt-image-1 takes 10 s.
    cover_title = (cover.get("title") or "").strip()
    cover_subtitle = (cover.get("body") or "").strip()
    full_prompt = await cover_prompt.write_cover_prompt(
        talking_point=talking_point,
        cover_title=cover_title,
    )
    log.info("carousel.render cover_prompt node=%s prompt=%r", node_id, full_prompt[:200])
    cover_png = await images.generate_cover_image(
        full_prompt,
        size=templates.COVER_IMAGE_GEN_SIZE,
    )
    cover_data_url = images.to_data_url(cover_png, mime="image/png")

    # Step 2: open a single Playwright session, render all slides.
    rendered: list[SlideRender] = []
    async with RenderSession.open() as session:
        # Cover slide always rendered first (its data URL is biggest, so
        # render it while DNS for fonts.googleapis.com is fresh).
        cover_html = templates.render_cover_slide_html(
            title=cover_title or "БЕЗ НАЗВАНИЯ",
            subtitle=cover_subtitle,
            tag=None,  # No eyebrow on the cover — it's the visual hook.
            image_data_url=cover_data_url,
        )
        cover_jpeg = await session.render_html_to_jpeg(cover_html)
        cover_url = _upload_slide(
            node_id=node_id,
            render_id=render_id,
            slide_number=1,
            jpeg_bytes=cover_jpeg,
        )
        rendered.append(
            SlideRender(
                index=cover_idx + 1,
                is_cover=True,
                url=cover_url,
                width=templates.SLIDE_WIDTH,
                height=templates.SLIDE_HEIGHT,
            )
        )

        # Body slides
        for display_idx, slide in enumerate(slides_data, start=1):
            if display_idx - 1 == cover_idx:
                continue  # already rendered
            body_html = _body_slide_html_for(
                slide=slide,
                display_idx=display_idx,
                total=total,
                brand=brand_visual,
            )
            body_jpeg = await session.render_html_to_jpeg(body_html)
            url = _upload_slide(
                node_id=node_id,
                render_id=render_id,
                slide_number=display_idx,
                jpeg_bytes=body_jpeg,
            )
            rendered.append(
                SlideRender(
                    index=display_idx,
                    is_cover=False,
                    url=url,
                    width=templates.SLIDE_WIDTH,
                    height=templates.SLIDE_HEIGHT,
                )
            )

    # Re-order by display index so the frontend can render the carousel
    # left-to-right without sorting.
    rendered.sort(key=lambda r: r.index)
    duration = asyncio.get_event_loop().time() - started

    return RenderResult(
        render_id=render_id,
        slides=rendered,
        cover_prompt=full_prompt,
        duration_seconds=round(duration, 2),
        style=brand_visual.style,
        meta={
            "slides_count": len(rendered),
            "cover_index_input": cover_idx + 1,
        },
    )


def _body_slide_html_for(
    *,
    slide: dict[str, Any],
    display_idx: int,
    total: int,
    brand: BrandVisual,
) -> str:
    """Construct the HTML for one body slide."""
    eyebrow_hint = (slide.get("eyebrow_hint") or brand.eyebrow_text).strip()
    eyebrow = templates.derive_eyebrow(display_idx, total, eyebrow_hint)

    title = templates.title_with_number(
        str(slide.get("title") or "").strip(),
        slide_index=display_idx,
        prepend_number=brand.prepend_slide_number,
    )
    body = str(slide.get("body") or "").strip()

    return templates.render_body_slide_html(
        eyebrow=eyebrow,
        title=title,
        body=body,
        username=brand.username if brand.username else None,
        slide_number=display_idx if brand.show_numbering else None,
        slide_total=total if brand.show_numbering else None,
    )


async def render_single_body_slide(
    *,
    node_id: str,
    render_id: str,
    slide_index_1based: int,
    slide_data: dict[str, Any],
    total_slides: int,
    brand_visual: BrandVisual,
) -> SlideRender:
    """Re-render a single body (non-cover) slide and upload it to S3.

    Used by `slide_tweak` per-slide conversational edits (Virale-style).
    Reuses the existing `render_id` so the slide stays grouped with the
    rest of the carousel in S3 / the ZIP archive — but the per-slide
    object gets a fresh uuid suffix in its key, so a re-render doesn't
    overwrite the previous bytes (useful for "compare with prev" UX
    later, plus avoids browser-cache staleness on the same URL).

    Cover slides are NOT supported by this path — their AI-image
    background isn't stored separately, so we can't re-compose them.
    Caller should reject `is_cover=True` slides upstream.
    """
    if slide_data.get("is_cover"):
        raise ValueError(
            "render_single_body_slide не поддерживает обложку — "
            "для cover-слайда используй полный re-render или rehook"
        )
    html = _body_slide_html_for(
        slide=slide_data,
        display_idx=slide_index_1based,
        total=total_slides,
        brand=brand_visual,
    )
    async with RenderSession.open() as session:
        jpeg = await session.render_html_to_jpeg(html)

    url = _upload_slide(
        node_id=node_id,
        render_id=render_id,
        slide_number=slide_index_1based,
        jpeg_bytes=jpeg,
    )
    return SlideRender(
        index=slide_index_1based,
        is_cover=False,
        url=url,
        width=templates.SLIDE_WIDTH,
        height=templates.SLIDE_HEIGHT,
    )


def _upload_slide(
    *,
    node_id: str,
    render_id: str,
    slide_number: int,
    jpeg_bytes: bytes,
) -> str:
    """Persist a single slide and return its public URL.

    Key shape: `renders/carousel/{node_id}/{render_id}/{N:02d}.jpg` — the
    short uuid `render_id` keeps re-renders separate so users can compare
    a tweak vs the previous version without overwriting.
    """
    return storage.save_public_bytes(
        jpeg_bytes,
        key_prefix=f"renders/carousel/{node_id}/{render_id}",
        suggested_name=f"{slide_number:02d}.jpg",
        content_type="image/jpeg",
    )
