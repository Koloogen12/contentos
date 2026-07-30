"""Carousel-render auxiliary endpoints: archive download, per-slide tweak."""
from __future__ import annotations

import io
import logging
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.config import settings
from app.models.canvas import Canvas, Node, SkillRun
from app.schemas.skill_run import SkillRunStarted
from app.services import storage
from app.workers.queue import get_arq_pool

logger = logging.getLogger(__name__)

router = APIRouter(tags=["render"])


@router.get("/nodes/{node_id}/render-archive")
async def download_render_archive(
    node_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
):
    """Bundle every JPEG of the latest carousel render into a ZIP.

    Same auth/ownership rules as other node endpoints: the node must
    belong to one of the user's organization's canvases. Returns a
    streamed `application/zip` so a 10-slide carousel (~3MB total) flows
    out without ballooning worker memory.

    Layout inside the archive:

        carousel-<short-id>/
          01.jpg
          02.jpg
          ...
          README.txt   (full talking_point + caption for context)

    We don't preserve the random-uuid prefix Selectel-side filenames have;
    inside the zip we just number sequentially. Easier to copy-paste into
    Instagram's "Add to post" picker.
    """
    node = await db.scalar(
        select(Node)
        .join(Canvas)
        .where(Node.id == node_id, Canvas.organization_id == current.organization_id)
    )
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Node not found")

    data: dict[str, Any] = node.data or {}
    rendered: dict[str, Any] | None = data.get("rendered_slides")
    if not rendered or not rendered.get("slides"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "На этой ноде ещё нет рендера — сначала запусти «Сгенерировать визуал»",
        )

    render_id: str = rendered.get("render_id") or "current"
    s3_prefix = f"renders/carousel/{node_id}/{render_id}/"

    # Pull bytes for every slide via boto3. Inline rather than threadpooled
    # because 10 sequential S3 GETs from the same DC take ~2s total — no
    # benefit from parallelism, and avoiding asyncio→thread bridging keeps
    # the response handler simple.
    s3 = storage._s3_client()  # noqa: SLF001 — internal helper, intentional
    try:
        listing = s3.list_objects_v2(Bucket=settings.S3_BUCKET, Prefix=s3_prefix)
    except Exception as exc:
        logger.exception("render-archive list failed")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Не удалось получить список слайдов из S3: {exc}",
        ) from exc

    objects = sorted(
        (o for o in listing.get("Contents", []) if o["Key"].endswith(".jpg")),
        # Slide number is the suffix before .jpg — sort lexicographically
        # works because we zero-pad to two digits (01..10).
        key=lambda o: o["Key"],
    )
    if not objects:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "В S3 нет файлов для этого рендера (возможно, бакет очистили)",
        )

    buf = io.BytesIO()
    short_id = render_id[:8]
    folder = f"carousel-{short_id}/"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, obj in enumerate(objects, start=1):
            try:
                resp = s3.get_object(Bucket=settings.S3_BUCKET, Key=obj["Key"])
                payload = resp["Body"].read()
            except Exception:
                logger.exception("render-archive: failed to read %s", obj["Key"])
                continue
            # Two-digit zero-padded so files sort the same in any picker.
            zf.writestr(f"{folder}{idx:02d}.jpg", payload)

        # README with caption / talking-point / CTA so the user has the
        # post text alongside the slides — same workflow Virale ships with.
        readme_lines: list[str] = []
        if data.get("talking_point_text"):
            readme_lines.append("ТЕЗИС\n")
            readme_lines.append(str(data["talking_point_text"]).strip())
            readme_lines.append("\n\n")
        if data.get("full_text"):
            readme_lines.append("ПОЛНЫЙ ТЕКСТ ДЛЯ ПУБЛИКАЦИИ\n")
            readme_lines.append(str(data["full_text"]).strip())
            readme_lines.append("\n")
        if readme_lines:
            zf.writestr(f"{folder}README.txt", "".join(readme_lines))

    buf.seek(0)

    filename = f"carousel-{short_id}.zip"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Modest cache — same render_id always yields the same archive,
            # so browsers can short-circuit re-downloads within a 5-min window.
            "Cache-Control": "private, max-age=300",
        },
    )


class SlideTweakRequest(BaseModel):
    """User-issued instruction to rewrite + re-render one carousel slide.

    `slide_index` is 1-based to match what users see in the UI (and what
    `rendered_slides.slides[*].index` carries). The cover slide rejects
    this flow entirely — caller's UI should hide / disable the edit
    affordance for is_cover slides.
    """

    slide_index: int = Field(ge=1, le=20)
    user_prompt: str = Field(min_length=1, max_length=500)


@router.post(
    "/nodes/{node_id}/render-slide-tweak",
    response_model=SkillRunStarted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def render_slide_tweak(
    node_id: uuid.UUID,
    payload: SlideTweakRequest,
    current: CurrentUser,
    db: DbSession,
) -> SkillRunStarted:
    """Kick off a per-slide AI rewrite + Playwright re-render.

    The actual work runs in `app/workers/tasks.slide_tweak`. We just
    validate ownership + pre-flight conditions here so the user gets a
    synchronous 400 for "no rendered_slides yet" / "cover not editable",
    instead of waiting for the queued job to fail with a generic error.
    """
    node = await db.scalar(
        select(Node)
        .join(Canvas)
        .where(Node.id == node_id, Canvas.organization_id == current.organization_id)
    )
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Node not found")
    if node.type != "format":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Slide-tweak только для format-нод"
        )

    data: dict[str, Any] = node.data or {}
    if (data.get("platform") or "") != "carousel":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Slide-tweak поддерживается только для carousel",
        )

    slides_list = data.get("slides") or []
    if payload.slide_index > len(slides_list):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Слайда {payload.slide_index} нет в этой карусели (всего {len(slides_list)})",
        )
    slide = slides_list[payload.slide_index - 1]
    if isinstance(slide, dict) and slide.get("is_cover"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Обложку через slide-tweak редактировать нельзя — используй «Другой хук» или полную перегенерацию визуала",
        )

    rendered = data.get("rendered_slides")
    if not rendered or not rendered.get("slides"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Сначала запусти «Сгенерировать визуал» — нечего редактировать",
        )

    # Slide-tweak combines an AI text rewrite + a Playwright re-render.
    # We charge it as BOTH an AI-run AND a render quota use, so trial
    # users can't fan-out edits indefinitely.
    from app.models.auth import Organization as _Org
    from app.services import trial as _trial
    org = await db.scalar(select(_Org).where(_Org.id == current.organization_id))
    if org is not None:
        await _trial.assert_ai_quota(db, org)
        await _trial.assert_render_quota(db, org)

    skill_run = SkillRun(
        node_id=node.id,
        skill="slide_tweak",
        status="pending",
        created_at=datetime.now(timezone.utc),
        input_snapshot={
            "slide_index": payload.slide_index,
            "user_prompt": payload.user_prompt.strip(),
        },
    )
    db.add(skill_run)
    if org is not None and _trial.is_trial(org):
        await _trial.incr_ai_usage(db, current.organization_id)
        await _trial.incr_render_usage(db, current.organization_id)
    await db.flush()

    pool = await get_arq_pool()
    await pool.enqueue_job("slide_tweak", str(skill_run.id))

    return SkillRunStarted(
        skill_run_id=skill_run.id,
        skill="slide_tweak",
        status="pending",
    )
