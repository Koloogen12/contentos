"""Content plan analytics: week summary, streak, content-mix, what-to-write."""
from __future__ import annotations

import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content_plan import PlannedPost
from app.models.knowledge import KnowledgeItem

# Target content mix (per CLAUDE.md)
TARGET_MIX = {"R1": 0.40, "R2": 0.20, "R3": 0.25, "R4": 0.15}

DAY_NAMES_RU = [
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
]


def week_bounds(reference: date) -> tuple[date, date]:
    """Monday → Sunday window covering `reference`."""
    monday = reference - timedelta(days=reference.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


async def list_posts_in_range(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    date_from: date,
    date_to: date,
) -> list[PlannedPost]:
    rows = await db.scalars(
        select(PlannedPost)
        .where(
            PlannedPost.organization_id == organization_id,
            PlannedPost.scheduled_date >= date_from,
            PlannedPost.scheduled_date <= date_to,
        )
        .order_by(PlannedPost.scheduled_date.asc(), PlannedPost.scheduled_time.asc().nullslast())
    )
    return list(rows.all())


async def build_week_summary(
    db: AsyncSession, *, organization_id: uuid.UUID, anchor: date
) -> dict[str, Any]:
    monday, sunday = week_bounds(anchor)
    posts = await list_posts_in_range(
        db, organization_id=organization_id, date_from=monday, date_to=sunday
    )

    by_day: dict[date, list[PlannedPost]] = {}
    for p in posts:
        if p.scheduled_date is None:
            continue
        by_day.setdefault(p.scheduled_date, []).append(p)

    days: list[dict[str, Any]] = []
    empty_days = 0
    for i in range(7):
        d = monday + timedelta(days=i)
        day_posts = by_day.get(d, [])
        if not day_posts:
            empty_days += 1
        days.append(
            {
                "date": d,
                "day_name": DAY_NAMES_RU[i],
                "posts": day_posts,
                "is_empty": len(day_posts) == 0,
            }
        )

    platforms: Counter[str] = Counter()
    pillars: Counter[str] = Counter()
    total_scheduled = 0
    for p in posts:
        platforms[p.platform] += 1
        if p.pillar:
            pillars[p.pillar] += 1
        if p.status == "scheduled":
            total_scheduled += 1

    # Ready posts in queue (no date) — needed for sidebar count
    ready_q = await db.scalars(
        select(PlannedPost).where(
            PlannedPost.organization_id == organization_id,
            PlannedPost.status == "ready",
            PlannedPost.scheduled_date.is_(None),
        )
    )
    total_ready = len(list(ready_q.all()))

    return {
        "week_start": monday,
        "week_end": sunday,
        "days": days,
        "stats": {
            "total_scheduled": total_scheduled,
            "total_ready": total_ready,
            "empty_days": empty_days,
            "platforms": dict(platforms),
            "pillars": dict(pillars),
        },
    }


async def compute_streak(
    db: AsyncSession, *, organization_id: uuid.UUID
) -> int:
    """Consecutive days (ending today) with at least one published post."""
    today = datetime.now(timezone.utc).date()
    rows = await db.execute(
        select(func.date(PlannedPost.published_at))
        .where(
            PlannedPost.organization_id == organization_id,
            PlannedPost.published_at.is_not(None),
        )
        .group_by(func.date(PlannedPost.published_at))
    )
    days_with_publish = {r[0] for r in rows.all() if r[0] is not None}

    streak = 0
    cursor = today
    # If today has nothing yet, the streak still counts back from yesterday.
    if cursor not in days_with_publish:
        cursor -= timedelta(days=1)
    while cursor in days_with_publish:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


async def build_stats(
    db: AsyncSession, *, organization_id: uuid.UUID
) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    monday, sunday = week_bounds(today)
    month_start = today.replace(day=1)

    rows = await db.scalars(
        select(PlannedPost).where(
            PlannedPost.organization_id == organization_id,
            PlannedPost.status == "published",
        )
    )
    published = list(rows.all())

    total = len(published)

    def in_range(p: PlannedPost, lo: date, hi: date) -> bool:
        anchor = (
            p.published_at.date() if p.published_at else p.scheduled_date
        )
        if anchor is None:
            return False
        return lo <= anchor <= hi

    this_week = sum(1 for p in published if in_range(p, monday, sunday))
    this_month = sum(1 for p in published if in_range(p, month_start, today))

    pillar_counts: Counter[str] = Counter(
        p.pillar for p in published if p.pillar
    )
    platform_counts: Counter[str] = Counter(p.platform for p in published)

    pillar_total = sum(pillar_counts.values()) or 1
    content_mix = {
        k: round(v * 100 / pillar_total) for k, v in pillar_counts.items()
    }
    # Make sure all four pillars are present in the response.
    for k in ("R1", "R2", "R3", "R4"):
        content_mix.setdefault(k, 0)

    plat_total = sum(platform_counts.values()) or 1
    platform_mix = {
        k: round(v * 100 / plat_total) for k, v in platform_counts.items()
    }

    streak = await compute_streak(db, organization_id=organization_id)

    # Top posts by metrics.saves (then views as fallback)
    def metric_score(p: PlannedPost) -> int:
        m = p.metrics or {}
        saves = int(m.get("saves") or 0)
        views = int(m.get("views") or 0)
        return saves * 10 + views // 100

    top_sorted = sorted(published, key=metric_score, reverse=True)[:5]
    top_posts = [
        {
            "id": p.id,
            "platform": p.platform,
            "hook": p.hook,
            "full_text": p.full_text,
            "pillar": p.pillar,
            "published_at": p.published_at,
            "metrics": p.metrics or {},
        }
        for p in top_sorted
    ]

    return {
        "publishing_streak": streak,
        "total_published": total,
        "this_week_published": this_week,
        "this_month_published": this_month,
        "content_mix": content_mix,
        "platform_mix": platform_mix,
        "top_posts": top_posts,
    }


async def what_to_write(
    db: AsyncSession, *, organization_id: uuid.UUID
) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    monday, sunday = week_bounds(today)

    week_rows = await db.scalars(
        select(PlannedPost).where(
            PlannedPost.organization_id == organization_id,
            PlannedPost.status == "published",
            PlannedPost.published_at.is_not(None),
            func.date(PlannedPost.published_at) >= monday,
            func.date(PlannedPost.published_at) <= sunday,
        )
    )
    week_published = list(week_rows.all())
    week_pillars: Counter[str] = Counter(p.pillar for p in week_published if p.pillar)
    total = sum(week_pillars.values()) or 1

    gaps = {
        pillar: TARGET_MIX[pillar] - (week_pillars.get(pillar, 0) / total)
        for pillar in TARGET_MIX
    }
    priority_pillar = max(gaps, key=gaps.get)

    # Dormant gem: viral_score >= 17, not used in 10+ days
    cutoff = datetime.now(timezone.utc) - timedelta(days=10)
    dormant_q = await db.scalars(
        select(KnowledgeItem)
        .where(
            KnowledgeItem.organization_id == organization_id,
            KnowledgeItem.viral_score.is_not(None),
            KnowledgeItem.viral_score >= 17,
            KnowledgeItem.is_dormant.is_(True),
        )
        .order_by(KnowledgeItem.viral_score.desc().nullslast())
        .limit(5)
    )
    dormant_items = list(dormant_q.all())
    # Filter by last_used_at <= cutoff in Python (some rows may have NULL)
    dormant_items = [
        i for i in dormant_items if i.last_used_at is None or i.last_used_at <= cutoff
    ]
    dormant_top = dormant_items[0] if dormant_items else None

    # Pillar-balance pick
    pillar_q = await db.scalars(
        select(KnowledgeItem)
        .where(
            KnowledgeItem.organization_id == organization_id,
            KnowledgeItem.pillar == priority_pillar,
            KnowledgeItem.viral_score.is_not(None),
            KnowledgeItem.viral_score >= 13,
        )
        .order_by(KnowledgeItem.viral_score.desc().nullslast())
        .limit(1)
    )
    pillar_top = pillar_q.first()

    # Top by score overall (excluding dormant pick if any)
    excluded = {dormant_top.id} if dormant_top else set()
    top_q = await db.scalars(
        select(KnowledgeItem)
        .where(
            KnowledgeItem.organization_id == organization_id,
            KnowledgeItem.viral_score.is_not(None),
            KnowledgeItem.viral_score >= 15,
        )
        .order_by(KnowledgeItem.viral_score.desc().nullslast())
        .limit(5)
    )
    top_items = [i for i in top_q.all() if i.id not in excluded]
    top_pick = top_items[0] if top_items else None

    def to_rec(kind: str, title: str, item: KnowledgeItem | None) -> dict[str, Any]:
        return {
            "type": kind,
            "title": title,
            "knowledge_item_id": item.id if item else None,
            "knowledge_item_title": item.title if item else None,
            "knowledge_item_body": item.body[:500] if item else None,
            "pillar": item.pillar if item and item.pillar else None,
            "viral_score": item.viral_score if item else None,
        }

    return {
        "date": today,
        "priority_pillar": priority_pillar,
        "pillar_reason": (
            f"На этой неделе {priority_pillar} меньше всего — "
            f"{int((week_pillars.get(priority_pillar, 0) / total) * 100)}% при цели "
            f"{int(TARGET_MIX[priority_pillar] * 100)}%"
        ),
        "recommendations": [
            to_rec("dormant_gem", "Забытый тезис с высоким потенциалом", dormant_top),
            to_rec("pillar_balance", f"Пора написать про {priority_pillar}", pillar_top),
            to_rec("top_score", "Просто лучший тезис в базе", top_pick),
        ],
    }
