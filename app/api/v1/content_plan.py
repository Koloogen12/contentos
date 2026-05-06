"""Content plan: PlannedPost CRUD + week/stats/queue + schedule-from-node."""
import uuid
from datetime import date as Date, datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.canvas import Canvas, Edge, Node
from app.models.content_plan import PlannedPost
from app.schemas.content_plan import (
    PillarT,
    PlannedPostCreate,
    PlannedPostOut,
    PlannedPostUpdate,
    PlatformT,
    PostStatusT,
    ScheduleFromNodeRequest,
    StatsResponse,
    TopPostOut,
    WeekDayOut,
    WeekResponse,
    WeekStatsOut,
    WhatToWriteResponse,
    WhatToWriteRecommendation,
)
from app.services.content_plan import (
    build_stats,
    build_week_summary,
    what_to_write,
)

router = APIRouter(prefix="/content-plan", tags=["content-plan"])


async def _owned(
    db, post_id: uuid.UUID, org_id: uuid.UUID
) -> PlannedPost:
    obj = await db.scalar(
        select(PlannedPost).where(
            PlannedPost.id == post_id,
            PlannedPost.organization_id == org_id,
        )
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Planned post not found")
    return obj


# ----- CRUD -----


@router.get("/posts", response_model=list[PlannedPostOut])
async def list_posts(
    current: CurrentUser,
    db: DbSession,
    date_from: Date | None = Query(default=None),
    date_to: Date | None = Query(default=None),
    status_filter: PostStatusT | None = Query(default=None, alias="status"),
    platform: PlatformT | None = Query(default=None),
    pillar: PillarT | None = Query(default=None),
    project_id: uuid.UUID | None = Query(default=None),
) -> list[PlannedPostOut]:
    stmt = select(PlannedPost).where(
        PlannedPost.organization_id == current.organization_id
    )
    if date_from is not None:
        stmt = stmt.where(PlannedPost.scheduled_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(PlannedPost.scheduled_date <= date_to)
    if status_filter is not None:
        stmt = stmt.where(PlannedPost.status == status_filter)
    if platform is not None:
        stmt = stmt.where(PlannedPost.platform == platform)
    if pillar is not None:
        stmt = stmt.where(PlannedPost.pillar == pillar)
    if project_id is not None:
        stmt = stmt.where(PlannedPost.project_id == project_id)

    stmt = stmt.order_by(
        PlannedPost.scheduled_date.asc().nullslast(),
        PlannedPost.scheduled_time.asc().nullslast(),
        PlannedPost.created_at.desc(),
    )
    rows = await db.scalars(stmt)
    return [PlannedPostOut.model_validate(r) for r in rows.all()]


@router.post(
    "/posts",
    response_model=PlannedPostOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_post(
    payload: PlannedPostCreate, current: CurrentUser, db: DbSession
) -> PlannedPostOut:
    new_status = payload.status
    if new_status == "draft" and payload.scheduled_date is not None:
        new_status = "scheduled"
    obj = PlannedPost(
        organization_id=current.organization_id,
        canvas_id=payload.canvas_id,
        node_id=payload.node_id,
        project_id=payload.project_id,
        platform=payload.platform,
        hook=payload.hook,
        body=payload.body,
        cta=payload.cta,
        full_text=payload.full_text,
        talking_point_text=payload.talking_point_text,
        scheduled_date=payload.scheduled_date,
        scheduled_time=payload.scheduled_time,
        status=new_status,
        pillar=payload.pillar,
        tags=payload.tags,
        notes=payload.notes,
        metrics=payload.metrics,
    )
    db.add(obj)
    await db.flush()
    return PlannedPostOut.model_validate(obj)


@router.get("/posts/{post_id}", response_model=PlannedPostOut)
async def get_post(
    post_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> PlannedPostOut:
    obj = await _owned(db, post_id, current.organization_id)
    return PlannedPostOut.model_validate(obj)


@router.patch("/posts/{post_id}", response_model=PlannedPostOut)
async def update_post(
    post_id: uuid.UUID,
    payload: PlannedPostUpdate,
    current: CurrentUser,
    db: DbSession,
) -> PlannedPostOut:
    obj = await _owned(db, post_id, current.organization_id)
    data = payload.model_dump(exclude_unset=True)
    user_set_status = "status" in data
    for field, value in data.items():
        setattr(obj, field, value)
    # Auto-promote when a date is added and the user didn't explicitly set
    # the status. draft → scheduled, ready → scheduled.
    if not user_set_status and obj.scheduled_date is not None and obj.status in ("draft", "ready"):
        obj.status = "scheduled"
    await db.flush()
    await db.refresh(obj)
    return PlannedPostOut.model_validate(obj)


@router.delete("/posts/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_post(
    post_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> None:
    obj = await _owned(db, post_id, current.organization_id)
    await db.delete(obj)


# ----- State transitions -----


@router.post("/posts/{post_id}/publish", response_model=PlannedPostOut)
async def mark_published(
    post_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> PlannedPostOut:
    obj = await _owned(db, post_id, current.organization_id)
    obj.status = "published"
    obj.published_at = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(obj)
    return PlannedPostOut.model_validate(obj)


@router.post("/posts/{post_id}/skip", response_model=PlannedPostOut)
async def mark_skipped(
    post_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> PlannedPostOut:
    obj = await _owned(db, post_id, current.organization_id)
    obj.status = "skipped"
    await db.flush()
    await db.refresh(obj)
    return PlannedPostOut.model_validate(obj)


# ----- Queue + Week + Stats -----


@router.get("/queue", response_model=list[PlannedPostOut])
async def queue(current: CurrentUser, db: DbSession) -> list[PlannedPostOut]:
    """Posts with status='ready' that have no scheduled_date — sidebar queue."""
    rows = await db.scalars(
        select(PlannedPost)
        .where(
            PlannedPost.organization_id == current.organization_id,
            PlannedPost.status == "ready",
            PlannedPost.scheduled_date.is_(None),
        )
        .order_by(PlannedPost.created_at.desc())
    )
    return [PlannedPostOut.model_validate(r) for r in rows.all()]


@router.get("/week", response_model=WeekResponse)
async def week_summary(
    current: CurrentUser,
    db: DbSession,
    date_from: Date | None = Query(default=None),
) -> WeekResponse:
    anchor = date_from or datetime.now(timezone.utc).date()
    summary = await build_week_summary(
        db, organization_id=current.organization_id, anchor=anchor
    )
    return WeekResponse(
        week_start=summary["week_start"],
        week_end=summary["week_end"],
        days=[
            WeekDayOut(
                date=d["date"],
                day_name=d["day_name"],
                posts=[PlannedPostOut.model_validate(p) for p in d["posts"]],
                is_empty=d["is_empty"],
            )
            for d in summary["days"]
        ],
        stats=WeekStatsOut(**summary["stats"]),
    )


@router.get("/stats", response_model=StatsResponse)
async def stats(current: CurrentUser, db: DbSession) -> StatsResponse:
    raw = await build_stats(db, organization_id=current.organization_id)
    return StatsResponse(
        publishing_streak=raw["publishing_streak"],
        publishing_streak_record=raw["publishing_streak_record"],
        total_published=raw["total_published"],
        this_week_published=raw["this_week_published"],
        this_month_published=raw["this_month_published"],
        content_mix=raw["content_mix"],
        platform_mix=raw["platform_mix"],
        top_posts=[TopPostOut(**p) for p in raw["top_posts"]],
    )


# ----- "What to write today" (lives under content-plan; also re-exposed in knowledge) -----


@router.get("/what-to-write", response_model=WhatToWriteResponse)
async def what_to_write_today(
    current: CurrentUser, db: DbSession
) -> WhatToWriteResponse:
    raw = await what_to_write(db, organization_id=current.organization_id)
    return WhatToWriteResponse(
        date=raw["date"],
        priority_pillar=raw["priority_pillar"],
        pillar_reason=raw["pillar_reason"],
        recommendations=[
            WhatToWriteRecommendation(**r) for r in raw["recommendations"]
        ],
    )


# ----- Schedule from a Format Node -----


schedule_router = APIRouter(tags=["content-plan"])


@schedule_router.post(
    "/nodes/{node_id}/schedule",
    response_model=PlannedPostOut,
    status_code=status.HTTP_201_CREATED,
)
async def schedule_from_node(
    node_id: uuid.UUID,
    payload: ScheduleFromNodeRequest,
    current: CurrentUser,
    db: DbSession,
) -> PlannedPostOut:
    """Copy a Format Node's data into a new PlannedPost.

    Reads the node's data (already produced by the format skill), pulls the
    upstream extract's selected talking_point text for context, and creates
    the PlannedPost with status `scheduled` (if a date was given) or `ready`
    otherwise.
    """
    node = await db.scalar(
        select(Node)
        .join(Canvas, Canvas.id == Node.canvas_id)
        .where(Node.id == node_id, Canvas.organization_id == current.organization_id)
    )
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Node not found")
    if node.type != "format":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Только format-ноду можно запланировать"
        )

    data = dict(node.data or {})
    full_text = (data.get("full_text") or "").strip()
    if not full_text:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "В ноде нет full_text — сначала сгенерируй пост.",
        )

    # Pull talking_point context from upstream extract (best-effort).
    talking_point_text = data.get("talking_point_text")
    if not talking_point_text:
        edge = await db.scalar(
            select(Edge).where(Edge.target_node_id == node.id)
        )
        if edge is not None:
            parent = await db.scalar(select(Node).where(Node.id == edge.source_node_id))
            if parent is not None and parent.type == "extract":
                tps = (parent.data or {}).get("talking_points") or []
                idx = (parent.data or {}).get("selected_index")
                if isinstance(idx, int) and 0 <= idx < len(tps):
                    talking_point_text = tps[idx].get("text")

    # Pick selected hook if multiple were generated.
    hook = ""
    hooks = data.get("hooks") or []
    sel = data.get("selected_hook_index", 0)
    if hooks and isinstance(sel, int) and 0 <= sel < len(hooks):
        hook = str(hooks[sel])
    elif data.get("hook"):
        hook = str(data.get("hook"))

    canvas = await db.scalar(select(Canvas).where(Canvas.id == node.canvas_id))
    project_id = canvas.project_id if canvas else None

    new_status: PostStatusT = (
        "scheduled" if payload.scheduled_date is not None else "ready"
    )

    post = PlannedPost(
        organization_id=current.organization_id,
        canvas_id=node.canvas_id,
        node_id=node.id,
        project_id=project_id,
        platform=str(data.get("platform") or "telegram"),
        hook=hook,
        body=str(data.get("body") or ""),
        cta=str(data.get("cta") or ""),
        full_text=full_text,
        talking_point_text=talking_point_text,
        scheduled_date=payload.scheduled_date,
        scheduled_time=payload.scheduled_time,
        status=new_status,
        pillar=payload.pillar,
        tags=payload.tags,
    )
    db.add(post)
    await db.flush()
    return PlannedPostOut.model_validate(post)
