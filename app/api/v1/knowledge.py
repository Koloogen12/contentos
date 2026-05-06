import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
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
async def delete_knowledge(item_id: uuid.UUID, current: CurrentUser, db: DbSession) -> None:
    obj = await _owned_item(db, item_id, current.organization_id)
    await db.delete(obj)


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
) -> None:
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
) -> None:
    await _owned_node(db, node_id, current.organization_id)
    link = await db.scalar(
        select(NodeKnowledge).where(
            NodeKnowledge.node_id == node_id,
            NodeKnowledge.knowledge_item_id == item_id,
        )
    )
    if link is not None:
        await db.delete(link)
