"""Publish a format node's full_text to a Telegram target."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.canvas import Canvas, Node
from app.models.publish import PublishLog, TelegramTarget
from app.schemas.publish import PublishLogOut, PublishStart, PublishStarted
from app.workers.queue import get_arq_pool

router = APIRouter(tags=["publish"])


async def _owned_format_node(db, node_id: uuid.UUID, org_id: uuid.UUID) -> Node:
    node = await db.scalar(
        select(Node).join(Canvas).where(Node.id == node_id, Canvas.organization_id == org_id)
    )
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Node not found")
    if node.type != "format":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only format nodes can be published")
    return node


@router.post(
    "/nodes/{node_id}/publish",
    response_model=PublishStarted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def publish_node(
    node_id: uuid.UUID,
    payload: PublishStart,
    current: CurrentUser,
    db: DbSession,
) -> PublishStarted:
    node = await _owned_format_node(db, node_id, current.organization_id)
    text = (node.data or {}).get("full_text") or ""
    if not text.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "В ноде нет full_text — сначала запусти Format.")

    target = await db.scalar(
        select(TelegramTarget).where(
            TelegramTarget.id == payload.target_id,
            TelegramTarget.organization_id == current.organization_id,
        )
    )
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Target not found")

    log = PublishLog(
        node_id=node.id,
        target_id=target.id,
        status="pending",
        text=text,
        created_at=datetime.now(timezone.utc),
    )
    db.add(log)
    await db.flush()

    pool = await get_arq_pool()
    await pool.enqueue_job("publish_to_telegram", str(log.id))

    return PublishStarted(publish_log_id=log.id, status="pending")


@router.get("/publish-logs/{publish_log_id}", response_model=PublishLogOut)
async def get_publish_log(
    publish_log_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
) -> PublishLogOut:
    log = await db.scalar(
        select(PublishLog)
        .join(Node, Node.id == PublishLog.node_id)
        .join(Canvas, Canvas.id == Node.canvas_id)
        .where(PublishLog.id == publish_log_id, Canvas.organization_id == current.organization_id)
    )
    if log is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PublishLog not found")
    return PublishLogOut.model_validate(log)
