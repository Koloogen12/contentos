"""Performance feedback loop (Sprint 3 — Task C, MVP).

Reads metrics that Track B (Telegram metrics auto-pull) has been
accumulating in `publish_logs.metrics` and turns them into actionable
intelligence:

  1. Compute the org's recent posting-performance baseline (median views)
  2. Classify each sent post into a tier (top / good / median / low)
  3. Surface top-tier posts as candidates for `voice_samples` so the
     few-shot retrieval at format time starts pulling from posts that
     actually performed (and not just from random imports)

This MVP version does NOT auto-update `KnowledgeItem.viral_score` —
that requires a node ↔ knowledge-item linkage we don't track yet. The
loop is "shown to user → user confirms → manual update". The fully
automatic loop is a follow-up sub-track.
"""
from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Node
from app.models.knowledge import VoiceSample
from app.models.publish import PublishLog, TelegramTarget


# Rolling window for baseline computation. 60 days balances "recent
# enough to reflect current niche" vs "enough samples for a stable
# median". Shrink if a user posts daily, grow if posts weekly — but for
# MVP one constant works.
_BASELINE_DAYS = 60

# Minimum samples to compute a meaningful baseline. With fewer than 3
# posts the median is noise — fall back to a "no baseline" path that
# still lets us classify on absolute views (≥5K = top, etc.).
_BASELINE_MIN_SAMPLES = 3

# Absolute view thresholds used when there's no statistical baseline.
# Calibrated for a typical small-to-mid Telegram channel (1K-10K subs).
_ABSOLUTE_TOP = 5000
_ABSOLUTE_GOOD = 2000
_ABSOLUTE_MEDIAN = 800


PerformanceTier = str  # "top" | "good" | "median" | "low" | "unknown"


@dataclass
class PostPerformance:
    """One sent post + classified tier + raw metrics. Returned by the
    performance overview endpoint, consumed by the frontend table."""

    publish_log_id: uuid.UUID
    node_id: uuid.UUID
    target_id: uuid.UUID
    target_title: str
    tier: PerformanceTier
    views: int | None
    forwards: int | None
    reactions_total: int
    text_preview: str
    full_text: str  # for voice-sample promotion
    metrics_fetched_at: str | None
    completed_at: str | None


@dataclass
class PerformanceOverview:
    """Headline stats + classified post list for the /performance page."""

    median_views: int | None
    total_posts: int
    top_count: int
    good_count: int
    median_count: int
    low_count: int
    unknown_count: int
    posts: list[PostPerformance]
    has_baseline: bool


def _classify(views: int | None, median: int | None) -> PerformanceTier:
    """Bucket one post's views into a tier.

    With a baseline:
      top    ≥ 1.5 × median
      good   1.0× — 1.5× median
      median 0.5× — 1.0× median
      low    < 0.5× median

    Without a baseline (no median yet), fall back to absolute thresholds.
    """
    if views is None or views <= 0:
        return "unknown"
    if median and median > 0:
        ratio = views / median
        if ratio >= 1.5:
            return "top"
        if ratio >= 1.0:
            return "good"
        if ratio >= 0.5:
            return "median"
        return "low"
    # No baseline — absolute thresholds.
    if views >= _ABSOLUTE_TOP:
        return "top"
    if views >= _ABSOLUTE_GOOD:
        return "good"
    if views >= _ABSOLUTE_MEDIAN:
        return "median"
    return "low"


async def compute_overview(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    limit: int = 50,
) -> PerformanceOverview:
    """Return the headline performance dashboard for an org.

    `limit` caps how many posts we return ranked by views desc; the
    median is computed over ALL sent posts in the rolling window
    (independent of the response cap), so a user with hundreds of posts
    still gets a stable baseline.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=_BASELINE_DAYS)

    # Pull every sent post in the window with views known.
    rows = list(
        (
            await db.scalars(
                select(PublishLog)
                .join(TelegramTarget, TelegramTarget.id == PublishLog.target_id)
                .join(Node, Node.id == PublishLog.node_id)
                .where(
                    TelegramTarget.organization_id == organization_id,
                    PublishLog.status == "sent",
                    PublishLog.completed_at.is_not(None),
                    PublishLog.completed_at >= cutoff,
                )
                .order_by(PublishLog.completed_at.desc())
            )
        ).all()
    )

    # Build a target_id → title map for fast lookup
    target_titles: dict[uuid.UUID, str] = {}
    if rows:
        tids = {r.target_id for r in rows}
        target_rows = await db.scalars(
            select(TelegramTarget).where(TelegramTarget.id.in_(tids))
        )
        for t in target_rows.all():
            target_titles[t.id] = t.title

    # Views array for median calc — skip posts where metrics never landed.
    views_known: list[int] = []
    for log in rows:
        m = log.metrics or {}
        v = m.get("views")
        if isinstance(v, int) and v > 0:
            views_known.append(v)

    median_views: int | None = None
    has_baseline = False
    if len(views_known) >= _BASELINE_MIN_SAMPLES:
        median_views = int(statistics.median(views_known))
        has_baseline = True

    posts: list[PostPerformance] = []
    counts = {"top": 0, "good": 0, "median": 0, "low": 0, "unknown": 0}
    for log in rows:
        m = log.metrics or {}
        v = m.get("views") if isinstance(m.get("views"), int) else None
        f = m.get("forwards") if isinstance(m.get("forwards"), int) else None
        reactions_total = 0
        reactions = m.get("reactions") or {}
        if isinstance(reactions, dict):
            for _, count in reactions.items():
                if isinstance(count, int):
                    reactions_total += count

        tier = _classify(v, median_views)
        counts[tier] += 1

        text = log.text or ""
        preview = text.replace("\n", " ").strip()[:120]
        posts.append(
            PostPerformance(
                publish_log_id=log.id,
                node_id=log.node_id,
                target_id=log.target_id,
                target_title=target_titles.get(log.target_id, "—"),
                tier=tier,
                views=v,
                forwards=f,
                reactions_total=reactions_total,
                text_preview=preview,
                full_text=text,
                metrics_fetched_at=m.get("fetched_at"),
                completed_at=log.completed_at.isoformat()
                if log.completed_at
                else None,
            )
        )

    # Sort: top first, then by views desc — what the user wants to see
    # immediately is "what's working".
    tier_rank = {"top": 0, "good": 1, "median": 2, "low": 3, "unknown": 4}
    posts.sort(key=lambda p: (tier_rank[p.tier], -(p.views or 0)))
    posts = posts[:limit]

    return PerformanceOverview(
        median_views=median_views,
        total_posts=len(rows),
        top_count=counts["top"],
        good_count=counts["good"],
        median_count=counts["median"],
        low_count=counts["low"],
        unknown_count=counts["unknown"],
        posts=posts,
        has_baseline=has_baseline,
    )


async def is_already_in_voice_samples(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    text: str,
) -> bool:
    """Dedup guard: avoid promoting the same post twice.

    Match by first 200 chars (same heuristic voice_importers uses
    internally for dedup). Cheap, no fancy similarity required since
    publish_log.text is a stable string.
    """
    if not text.strip():
        return True  # treat empty as "already there" → refuse to add
    prefix = text.strip()[:200]
    existing = await db.scalar(
        select(VoiceSample)
        .where(
            VoiceSample.organization_id == organization_id,
            VoiceSample.text.ilike(f"{prefix}%"),
        )
        .limit(1)
    )
    return existing is not None


def promotion_meta(post: PostPerformance) -> dict[str, Any]:
    """Build the `meta` JSONB to store on a VoiceSample row, so we can
    later trace which post was promoted and why."""
    return {
        "source": "performance_promotion",
        "publish_log_id": str(post.publish_log_id),
        "node_id": str(post.node_id),
        "tier_at_promotion": post.tier,
        "views_at_promotion": post.views,
        "forwards_at_promotion": post.forwards,
        "reactions_at_promotion": post.reactions_total,
        "promoted_at": datetime.now(timezone.utc).isoformat(),
    }
