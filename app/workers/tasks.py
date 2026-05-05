"""Arq task entry points: skill execution + Telegram publishing."""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models.canvas import Node, SkillRun
from app.models.publish import PublishLog, TelegramTarget
from app.services import events, telegram_bot
from app.services.brand_context import build_skill_context, collect_input_for_skill
from app.services.skills import get as get_skill

logger = logging.getLogger(__name__)


async def _publish(skill_run_id: uuid.UUID, event: str, data: dict[str, Any]) -> None:
    try:
        await events.publish(skill_run_id, event, data)
    except Exception:
        logger.exception("publish event failed")


async def run_skill(ctx: dict, skill_run_id_str: str) -> dict[str, Any]:
    """Pull the SkillRun, dispatch to the matching skill, persist node + status."""
    skill_run_id = uuid.UUID(skill_run_id_str)

    async with SessionLocal() as db:
        skill_run = await db.scalar(select(SkillRun).where(SkillRun.id == skill_run_id))
        if skill_run is None:
            return {"ok": False, "error": "SkillRun not found"}

        node = await db.scalar(select(Node).where(Node.id == skill_run.node_id))
        if node is None:
            skill_run.status = "failed"
            skill_run.error = "Node not found"
            skill_run.completed_at = datetime.now(timezone.utc)
            await db.commit()
            await _publish(skill_run_id, "error", {"message": "Node not found"})
            return {"ok": False, "error": "Node not found"}

        canvas_id = node.canvas_id
        from app.models.canvas import Canvas

        canvas = await db.scalar(select(Canvas).where(Canvas.id == canvas_id))
        organization_id = canvas.organization_id if canvas else None

        skill_run.status = "running"
        node.status = "running"
        await db.commit()
        await _publish(skill_run_id, "status", {"status": "running"})

        started = time.perf_counter()
        try:
            # If the API endpoint already pre-loaded input_snapshot (e.g. for
            # transcription, where there's no upstream edge), use it as-is.
            # Otherwise walk the incoming edge to collect input from upstream.
            if skill_run.input_snapshot:
                skill_input = dict(skill_run.input_snapshot)
            else:
                skill_input = await collect_input_for_skill(db, node)
                if "error" in skill_input:
                    raise ValueError(skill_input["error"])
                skill_run.input_snapshot = skill_input

            system_context = await build_skill_context(
                db,
                organization_id=organization_id,
                canvas_id=canvas_id,
                node_id=node.id,
            )

            skill_fn = get_skill(skill_run.skill)
            await _publish(skill_run_id, "progress", {"step": "calling-ai"})
            result = await skill_fn(db, node, system_context, skill_input)

            new_data = result.get("node_data") or {}
            node.data = new_data
            node.status = "done"

            duration_ms = int((time.perf_counter() - started) * 1000)
            skill_run.status = "completed"
            skill_run.duration_ms = duration_ms
            skill_run.completed_at = datetime.now(timezone.utc)
            skill_run.output = None
            await db.commit()

            await _publish(
                skill_run_id,
                "complete",
                {
                    "node_id": str(node.id),
                    "node_data": new_data,
                    "node_status": "done",
                    "duration_ms": duration_ms,
                    "meta": result.get("meta") or {},
                },
            )
            return {"ok": True}
        except Exception as exc:
            logger.exception("skill run failed")
            duration_ms = int((time.perf_counter() - started) * 1000)
            await db.rollback()

            skill_run = await db.scalar(select(SkillRun).where(SkillRun.id == skill_run_id))
            node = await db.scalar(select(Node).where(Node.id == skill_run.node_id)) if skill_run else None
            if skill_run is not None:
                skill_run.status = "failed"
                skill_run.error = str(exc)[:2000]
                skill_run.duration_ms = duration_ms
                skill_run.completed_at = datetime.now(timezone.utc)
            if node is not None:
                node.status = "error"
            await db.commit()
            await _publish(skill_run_id, "error", {"message": str(exc)[:500]})
            return {"ok": False, "error": str(exc)}


async def publish_to_telegram(ctx: dict, publish_log_id_str: str) -> dict[str, Any]:
    """Send the format node's full_text to a TelegramTarget. Updates PublishLog."""
    publish_log_id = uuid.UUID(publish_log_id_str)

    async with SessionLocal() as db:
        log = await db.scalar(select(PublishLog).where(PublishLog.id == publish_log_id))
        if log is None:
            return {"ok": False, "error": "PublishLog not found"}

        target = await db.scalar(select(TelegramTarget).where(TelegramTarget.id == log.target_id))
        if target is None:
            log.status = "failed"
            log.error = "Target not found"
            log.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return {"ok": False, "error": "Target not found"}

        log.status = "sending"
        await db.commit()

        try:
            response = await telegram_bot.send_message(target, log.text)
            log.status = "sent"
            log.response = response
            log.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return {"ok": True}
        except Exception as exc:
            logger.exception("telegram publish failed")
            log.status = "failed"
            log.error = str(exc)[:2000]
            log.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return {"ok": False, "error": str(exc)}
