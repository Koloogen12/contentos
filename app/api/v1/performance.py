"""Performance dashboard + voice-sample promotion endpoints (Task C)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.knowledge import VoiceSample
from app.models.publish import PublishLog, TelegramTarget
from app.schemas.voice import VoiceSampleOut
from app.services import performance as perf_svc

router = APIRouter(prefix="/performance", tags=["performance"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PostPerformanceOut(BaseModel):
    """One sent post with its classified tier — main row shape of the
    performance dashboard."""

    publish_log_id: uuid.UUID
    node_id: uuid.UUID
    target_id: uuid.UUID
    target_title: str
    tier: str  # top | good | median | low | unknown
    views: int | None
    forwards: int | None
    reactions_total: int
    text_preview: str
    metrics_fetched_at: str | None
    completed_at: str | None


class PerformanceOverviewOut(BaseModel):
    median_views: int | None
    total_posts: int
    top_count: int
    good_count: int
    median_count: int
    low_count: int
    unknown_count: int
    posts: list[PostPerformanceOut]
    has_baseline: bool


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/overview", response_model=PerformanceOverviewOut)
async def get_overview(current: CurrentUser, db: DbSession) -> PerformanceOverviewOut:
    """Dashboard data for `/performance`.

    Returns the org's recent posting performance baseline (median views
    over the last 60 days), tier classification of recent posts, and
    counts per tier. The frontend uses this to surface candidates for
    voice-sample promotion and to give the user a sense of what content
    actually lands.
    """
    overview = await perf_svc.compute_overview(
        db, organization_id=current.organization_id, limit=50
    )
    return PerformanceOverviewOut(
        median_views=overview.median_views,
        total_posts=overview.total_posts,
        top_count=overview.top_count,
        good_count=overview.good_count,
        median_count=overview.median_count,
        low_count=overview.low_count,
        unknown_count=overview.unknown_count,
        has_baseline=overview.has_baseline,
        posts=[
            PostPerformanceOut(
                publish_log_id=p.publish_log_id,
                node_id=p.node_id,
                target_id=p.target_id,
                target_title=p.target_title,
                tier=p.tier,
                views=p.views,
                forwards=p.forwards,
                reactions_total=p.reactions_total,
                text_preview=p.text_preview,
                metrics_fetched_at=p.metrics_fetched_at,
                completed_at=p.completed_at,
            )
            for p in overview.posts
        ],
    )


@router.post(
    "/promote-to-voice/{publish_log_id}",
    response_model=VoiceSampleOut,
    status_code=status.HTTP_201_CREATED,
)
async def promote_to_voice_sample(
    publish_log_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
) -> VoiceSampleOut:
    """Add a high-performing post's text to `voice_samples`.

    The new sample is embedded asynchronously by the same path the
    /voice import flow uses, so it shows up in few-shot retrieval the
    next time the user generates a post. We tag the sample's `meta`
    with the source publish_log + tier so we can later show "this
    sample came from your top-3 carousel of June 2026" in the UI.

    Refuses to add a duplicate (same first-200-char prefix already
    present) with a 409.
    """
    # Ownership check via the join chain: publish_log → target → org
    log = await db.scalar(
        select(PublishLog)
        .join(TelegramTarget, TelegramTarget.id == PublishLog.target_id)
        .where(
            PublishLog.id == publish_log_id,
            TelegramTarget.organization_id == current.organization_id,
        )
    )
    if log is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PublishLog not found")
    if log.status != "sent":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "В voice samples можно добавлять только успешно опубликованные посты",
        )
    text = (log.text or "").strip()
    if not text:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "У поста пустой текст — нечего добавлять",
        )

    if await perf_svc.is_already_in_voice_samples(
        db, organization_id=current.organization_id, text=text
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Этот пост уже в голосовых образцах",
        )

    # Compute current tier on-the-fly so the promotion metadata reflects
    # actual performance at promotion time (not at publish time).
    overview = await perf_svc.compute_overview(
        db, organization_id=current.organization_id, limit=200
    )
    matched: perf_svc.PostPerformance | None = None
    for p in overview.posts:
        if p.publish_log_id == publish_log_id:
            matched = p
            break
    if matched is None:
        # Outside the 60-day window — still allow promotion, just no tier
        # context. Build a stub PostPerformance for metadata.
        matched = perf_svc.PostPerformance(
            publish_log_id=log.id,
            node_id=log.node_id,
            target_id=log.target_id,
            target_title="—",
            tier="unknown",
            views=(log.metrics or {}).get("views") if log.metrics else None,
            forwards=(log.metrics or {}).get("forwards") if log.metrics else None,
            reactions_total=0,
            text_preview=text[:120],
            full_text=text,
            metrics_fetched_at=(log.metrics or {}).get("fetched_at")
            if log.metrics
            else None,
            completed_at=log.completed_at.isoformat() if log.completed_at else None,
        )

    # Embed inline using the same helper voice.py uses. We import lazily
    # to avoid pulling the AI client into module-load (slow startup).
    from app.api.v1.voice import _embed_safely  # noqa: SLF001 — reuse internal

    embedding = await _embed_safely(text)

    # Resolve platform: the source target is Telegram (PublishLog → TelegramTarget).
    sample = VoiceSample(
        organization_id=current.organization_id,
        project_id=None,
        platform="telegram",
        text=text,
        embedding=embedding,
        meta=perf_svc.promotion_meta(matched),
    )
    db.add(sample)
    await db.flush()

    # Reuse voice.py's `_to_out` shape so the frontend doesn't need a
    # separate serializer.
    return VoiceSampleOut(
        id=sample.id,
        organization_id=sample.organization_id,
        project_id=sample.project_id,
        platform=sample.platform,
        text=sample.text,
        meta=sample.meta,
        has_embedding=sample.embedding is not None,
        created_at=sample.created_at,
        updated_at=sample.updated_at,
    )
