import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.canvas import Canvas, Node
from app.models.knowledge import KnowledgeItem, NodeKnowledge
from app.schemas.content_plan import (
    WhatToWriteRecommendation,
    WhatToWriteResponse,
)
from app.schemas.knowledge import (
    KnowledgeItemCreate,
    KnowledgeItemOut,
    KnowledgeItemUpdate,
    KnowledgeTypeT,
)
from app.services import brand_context as brand_context_svc
from app.services import brain_dump as brain_dump_svc
from app.services.content_plan import what_to_write as svc_what_to_write

router = APIRouter(tags=["knowledge"])


async def _owned_item(db, item_id: uuid.UUID, org_id: uuid.UUID) -> KnowledgeItem:
    obj = await db.scalar(
        select(KnowledgeItem).where(
            KnowledgeItem.id == item_id, KnowledgeItem.organization_id == org_id
        )
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Knowledge item not found")
    return obj


async def _owned_node(db, node_id: uuid.UUID, org_id: uuid.UUID) -> Node:
    node = await db.scalar(
        select(Node).join(Canvas).where(Node.id == node_id, Canvas.organization_id == org_id)
    )
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Node not found")
    return node


@router.get("/knowledge/what-to-write", response_model=WhatToWriteResponse)
async def what_to_write_today(
    current: CurrentUser, db: DbSession
) -> WhatToWriteResponse:
    raw = await svc_what_to_write(db, organization_id=current.organization_id)
    return WhatToWriteResponse(
        date=raw["date"],
        priority_pillar=raw["priority_pillar"],
        pillar_reason=raw["pillar_reason"],
        recommendations=[
            WhatToWriteRecommendation(**r) for r in raw["recommendations"]
        ],
    )


@router.get("/knowledge/dormant", response_model=list[KnowledgeItemOut])
async def list_dormant(current: CurrentUser, db: DbSession) -> list[KnowledgeItemOut]:
    rows = await db.scalars(
        select(KnowledgeItem)
        .where(
            KnowledgeItem.organization_id == current.organization_id,
            KnowledgeItem.is_dormant.is_(True),
        )
        .order_by(KnowledgeItem.last_used_at.asc().nullsfirst())
    )
    return [KnowledgeItemOut.model_validate(r) for r in rows.all()]


@router.get("/knowledge", response_model=list[KnowledgeItemOut])
async def list_knowledge(
    current: CurrentUser,
    db: DbSession,
    type: KnowledgeTypeT | None = Query(default=None),
    project_id: uuid.UUID | None = Query(default=None),
    is_dormant: bool | None = Query(default=None),
) -> list[KnowledgeItemOut]:
    stmt = select(KnowledgeItem).where(
        KnowledgeItem.organization_id == current.organization_id
    )
    if type is not None:
        stmt = stmt.where(KnowledgeItem.type == type)
    if project_id is not None:
        stmt = stmt.where(KnowledgeItem.project_id == project_id)
    if is_dormant is not None:
        stmt = stmt.where(KnowledgeItem.is_dormant.is_(is_dormant))
    stmt = stmt.order_by(KnowledgeItem.viral_score.desc().nullslast(), KnowledgeItem.created_at.desc())

    rows = await db.scalars(stmt)
    return [KnowledgeItemOut.model_validate(r) for r in rows.all()]


@router.post("/knowledge", response_model=KnowledgeItemOut, status_code=status.HTTP_201_CREATED)
async def create_knowledge(
    payload: KnowledgeItemCreate, current: CurrentUser, db: DbSession
) -> KnowledgeItemOut:
    obj = KnowledgeItem(
        organization_id=current.organization_id,
        project_id=payload.project_id,
        type=payload.type,
        title=payload.title,
        body=payload.body,
        tags=payload.tags,
        viral_score=payload.viral_score,
        pillar=payload.pillar,
        source_file=payload.source_file,
        is_dormant=False,
    )
    db.add(obj)
    await db.flush()
    return KnowledgeItemOut.model_validate(obj)


@router.patch("/knowledge/{item_id}", response_model=KnowledgeItemOut)
async def update_knowledge(
    item_id: uuid.UUID,
    payload: KnowledgeItemUpdate,
    current: CurrentUser,
    db: DbSession,
) -> KnowledgeItemOut:
    obj = await _owned_item(db, item_id, current.organization_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    await db.flush()
    return KnowledgeItemOut.model_validate(obj)


@router.delete("/knowledge/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge(item_id: uuid.UUID, current: CurrentUser, db: DbSession):
    obj = await _owned_item(db, item_id, current.organization_id)
    await db.delete(obj)


# ----- Bulk operations -----


from pydantic import BaseModel


class BulkDeleteRequest(BaseModel):
    ids: list[uuid.UUID]


class BulkUpdateProjectRequest(BaseModel):
    ids: list[uuid.UUID]
    project_id: uuid.UUID | None = None


class BulkResult(BaseModel):
    affected: int


@router.post("/knowledge/bulk-delete", response_model=BulkResult)
async def bulk_delete(
    payload: BulkDeleteRequest, current: CurrentUser, db: DbSession
) -> BulkResult:
    if not payload.ids:
        return BulkResult(affected=0)
    rows = await db.scalars(
        select(KnowledgeItem).where(
            KnowledgeItem.id.in_(payload.ids),
            KnowledgeItem.organization_id == current.organization_id,
        )
    )
    items = list(rows.all())
    for it in items:
        await db.delete(it)
    return BulkResult(affected=len(items))


@router.post("/knowledge/bulk-update-project", response_model=BulkResult)
async def bulk_update_project(
    payload: BulkUpdateProjectRequest, current: CurrentUser, db: DbSession
) -> BulkResult:
    if not payload.ids:
        return BulkResult(affected=0)
    rows = await db.scalars(
        select(KnowledgeItem).where(
            KnowledgeItem.id.in_(payload.ids),
            KnowledgeItem.organization_id == current.organization_id,
        )
    )
    items = list(rows.all())
    for it in items:
        it.project_id = payload.project_id
    return BulkResult(affected=len(items))


# Node ↔ Knowledge attachment


@router.get("/nodes/{node_id}/knowledge", response_model=list[KnowledgeItemOut])
async def list_node_knowledge(
    node_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> list[KnowledgeItemOut]:
    await _owned_node(db, node_id, current.organization_id)
    stmt = (
        select(KnowledgeItem)
        .join(NodeKnowledge, NodeKnowledge.knowledge_item_id == KnowledgeItem.id)
        .where(NodeKnowledge.node_id == node_id)
        .order_by(NodeKnowledge.attached_at.desc())
    )
    rows = await db.scalars(stmt)
    return [KnowledgeItemOut.model_validate(r) for r in rows.all()]


@router.post("/nodes/{node_id}/knowledge/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def attach_knowledge(
    node_id: uuid.UUID,
    item_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
):
    await _owned_node(db, node_id, current.organization_id)
    item = await _owned_item(db, item_id, current.organization_id)

    existing = await db.scalar(
        select(NodeKnowledge).where(
            NodeKnowledge.node_id == node_id,
            NodeKnowledge.knowledge_item_id == item_id,
        )
    )
    if existing is None:
        link = NodeKnowledge(
            node_id=node_id,
            knowledge_item_id=item_id,
            attached_at=datetime.now(timezone.utc),
        )
        db.add(link)

    item.last_used_at = datetime.now(timezone.utc)
    item.is_dormant = False


@router.delete("/nodes/{node_id}/knowledge/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def detach_knowledge(
    node_id: uuid.UUID,
    item_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
):
    await _owned_node(db, node_id, current.organization_id)
    link = await db.scalar(
        select(NodeKnowledge).where(
            NodeKnowledge.node_id == node_id,
            NodeKnowledge.knowledge_item_id == item_id,
        )
    )
    if link is not None:
        await db.delete(link)


# ---------------------------------------------------------------------------
# Brain dump → tezis proposals (NOT auto-saved)
# ---------------------------------------------------------------------------


class BrainDumpRequest(BaseModel):
    """User pastes free-form text. We DO NOT persist anything here —
    we just return AI-parsed tezis proposals. The frontend renders them
    as cards with `Save` buttons; saved ones become real KnowledgeItems
    via the existing `POST /knowledge` endpoint."""

    text: str = Field(min_length=5, max_length=8000)


class BrainDumpProposal(BaseModel):
    """One AI-parsed candidate tezis. Shape mirrors `KnowledgeItemCreate`
    fields so the frontend can pass it through `POST /knowledge` mostly
    unchanged (just adds `type='tezis'` if absent)."""

    title: str
    body: str
    viral_score: int
    pillar: str | None
    tags: list[str]


class BrainDumpResponse(BaseModel):
    proposals: list[BrainDumpProposal]


async def _gate_trial_ai(db, org_id):
    """Local copy of skill_runs._gate_ai_quota — avoids cross-file import."""
    from app.models.auth import Organization
    from app.services import trial as trial_svc

    org = await db.scalar(select(Organization).where(Organization.id == org_id))
    if org is not None:
        await trial_svc.assert_ai_quota(db, org)
        if trial_svc.is_trial(org):
            await trial_svc.incr_ai_usage(db, org_id)


@router.post("/knowledge/brain-dump", response_model=BrainDumpResponse)
async def brain_dump_to_tezis(
    payload: BrainDumpRequest,
    current: CurrentUser,
    db: DbSession,
) -> BrainDumpResponse:
    """Parse free-form thought / paragraph / chat-snippet into 3-7 tezis.

    Friction-reducer: the user has an idea and wants it in the bank
    without first uploading a YouTube transcript or running a full
    extract node. AI handles the parsing + scoring + pillar mapping;
    the user picks which proposals to keep.

    The endpoint is intentionally idempotent (no DB writes here) — so
    the user can re-call with refined text until the proposals look
    right, then explicitly save the ones they want via
    `POST /knowledge`.
    """
    await _gate_trial_ai(db, current.organization_id)

    system_context = await brand_context_svc.build_skill_context(
        db,
        organization_id=current.organization_id,
    )
    try:
        proposals_raw = await brain_dump_svc.parse_brain_dump(
            system_context=system_context,
            text=payload.text,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"AI не справился с разбором brain-dump: {exc}",
        ) from exc

    return BrainDumpResponse(
        proposals=[BrainDumpProposal(**p) for p in proposals_raw],
    )
