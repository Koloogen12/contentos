"""Lightweight ILIKE-based global search across knowledge, canvases, planned posts.

For MVP this is good enough. A V2 upgrade can replace the ILIKE filters
with a tsvector + GIN index without changing the contract.
"""
import uuid
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import or_, select

from app.api.deps import CurrentUser, DbSession
from app.models.canvas import Canvas
from app.models.content_plan import PlannedPost
from app.models.knowledge import KnowledgeItem

router = APIRouter(tags=["search"])


SearchKind = Literal["knowledge", "canvas", "planned_post"]


class SearchHit(BaseModel):
    kind: SearchKind
    id: uuid.UUID
    title: str
    snippet: str
    extra: dict | None = None


class SearchResponse(BaseModel):
    query: str
    hits: list[SearchHit]
    total: int


@router.get("/search", response_model=SearchResponse)
async def search(
    current: CurrentUser,
    db: DbSession,
    q: str = Query(..., min_length=2, max_length=200),
    kind: SearchKind | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
) -> SearchResponse:
    pattern = f"%{q.strip()}%"
    hits: list[SearchHit] = []

    if kind in (None, "knowledge"):
        rows = await db.scalars(
            select(KnowledgeItem)
            .where(
                KnowledgeItem.organization_id == current.organization_id,
                or_(
                    KnowledgeItem.title.ilike(pattern),
                    KnowledgeItem.body.ilike(pattern),
                ),
            )
            .order_by(KnowledgeItem.viral_score.desc().nullslast())
            .limit(limit)
        )
        for r in rows.all():
            hits.append(
                SearchHit(
                    kind="knowledge",
                    id=r.id,
                    title=r.title,
                    snippet=(r.body or "")[:200],
                    extra={
                        "type": r.type,
                        "viral_score": r.viral_score,
                        "pillar": r.pillar,
                    },
                )
            )

    if kind in (None, "canvas"):
        rows = await db.scalars(
            select(Canvas)
            .where(
                Canvas.organization_id == current.organization_id,
                or_(
                    Canvas.name.ilike(pattern),
                    Canvas.description.ilike(pattern),
                ),
            )
            .order_by(Canvas.updated_at.desc())
            .limit(limit)
        )
        for r in rows.all():
            hits.append(
                SearchHit(
                    kind="canvas",
                    id=r.id,
                    title=r.name,
                    snippet=(r.description or "")[:200],
                    extra={"is_template": r.is_template},
                )
            )

    if kind in (None, "planned_post"):
        rows = await db.scalars(
            select(PlannedPost)
            .where(
                PlannedPost.organization_id == current.organization_id,
                or_(
                    PlannedPost.hook.ilike(pattern),
                    PlannedPost.body.ilike(pattern),
                    PlannedPost.full_text.ilike(pattern),
                ),
            )
            .order_by(PlannedPost.scheduled_date.desc().nullslast())
            .limit(limit)
        )
        for r in rows.all():
            hits.append(
                SearchHit(
                    kind="planned_post",
                    id=r.id,
                    title=(r.hook or r.full_text[:80] or "Без названия"),
                    snippet=(r.full_text or "")[:200],
                    extra={
                        "platform": r.platform,
                        "status": r.status,
                        "pillar": r.pillar,
                        "scheduled_date": r.scheduled_date.isoformat()
                        if r.scheduled_date
                        else None,
                    },
                )
            )

    return SearchResponse(query=q, hits=hits[:limit], total=len(hits))
