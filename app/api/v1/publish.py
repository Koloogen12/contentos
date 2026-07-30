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


@router.get("/nodes/{node_id}/publish-logs", response_model=list[PublishLogOut])
async def list_node_publish_logs(
    node_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
) -> list[PublishLogOut]:
    """All publish attempts for this node, newest-first.

    Used by the FormatNode to render the metrics chip (views + reactions)
    next to the publish action. We return every publish_log (sent/failed/
    pending) so the UI can show "Failed" badges too — filtering to status
    happens client-side.
    """
    rows = await db.scalars(
        select(PublishLog)
        .join(Node, Node.id == PublishLog.node_id)
        .join(Canvas, Canvas.id == Node.canvas_id)
        .where(
            PublishLog.node_id == node_id,
            Canvas.organization_id == current.organization_id,
        )
        .order_by(PublishLog.created_at.desc())
    )
    return [PublishLogOut.model_validate(r) for r in rows.all()]


@router.post(
    "/publish-logs/{publish_log_id}/refresh-metrics",
    response_model=PublishLogOut,
)
async def refresh_publish_log_metrics(
    publish_log_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
) -> PublishLogOut:
    """Synchronously refresh metrics for one publish_log.

    The cron-driven `pull_telegram_metrics_all` sweep keeps metrics fresh
    on a 6h cadence; this endpoint lets the user pull a single post on
    demand (e.g. "I just posted — what's the count now?"). Runs in the
    request handler rather than the worker because the fetch is a single
    HTTP call to t.me — ~500ms typical, no reason to queue it.
    """
    log = await db.scalar(
        select(PublishLog)
        .join(Node, Node.id == PublishLog.node_id)
        .join(Canvas, Canvas.id == Node.canvas_id)
        .where(
            PublishLog.id == publish_log_id,
            Canvas.organization_id == current.organization_id,
        )
    )
    if log is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PublishLog not found")
    if log.status != "sent":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Метрики доступны только для отправленных постов (текущий статус: {log.status})",
        )

    msg_id = (log.response or {}).get("message_id")
    if not isinstance(msg_id, int):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "В response отсутствует message_id — невозможно построить URL для t.me",
        )

    target = await db.scalar(
        select(TelegramTarget).where(TelegramTarget.id == log.target_id)
    )
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Target deleted")

    from app.services import telegram_metrics

    handle = telegram_metrics.derive_handle(
        chat_id=target.chat_id,
        public_handle=target.public_handle,
    )
    if not handle:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "У этого канала нет публичного handle — добавь его в настройках цели",
        )

    metrics = await telegram_metrics.fetch_post_metrics(
        channel=handle,
        message_id=msg_id,
    )
    if metrics is None:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Не удалось получить метрики поста (пост удалён, t.me недоступен или embed заблокирован)",
        )

    log.metrics = metrics
    await db.flush()
    return PublishLogOut.model_validate(log)
