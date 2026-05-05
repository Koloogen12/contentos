"""Skill execution endpoints: start a run, poll, or stream events via SSE."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from app.api.deps import CurrentUser, CurrentUserSse, DbSession
from app.models.canvas import Canvas, Edge, Node, SkillRun
from app.schemas.skill_run import SkillRunOut, SkillRunStarted
from app.services import events
from app.services.skills import skill_for_node
from app.workers.queue import get_arq_pool

router = APIRouter(tags=["skill-runs"])


class BulkRunStarted(BaseModel):
    skill_runs: list[SkillRunStarted]
    skipped: int


async def _get_owned_node(db, node_id: uuid.UUID, org_id: uuid.UUID) -> Node:
    node = await db.scalar(
        select(Node).join(Canvas).where(Node.id == node_id, Canvas.organization_id == org_id)
    )
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Node not found")
    return node


async def _get_owned_skill_run(db, skill_run_id: uuid.UUID, org_id: uuid.UUID) -> SkillRun:
    sr = await db.scalar(
        select(SkillRun)
        .join(Node, Node.id == SkillRun.node_id)
        .join(Canvas, Canvas.id == Node.canvas_id)
        .where(SkillRun.id == skill_run_id, Canvas.organization_id == org_id)
    )
    if sr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SkillRun not found")
    return sr


@router.post(
    "/nodes/{node_id}/run",
    response_model=SkillRunStarted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_node(
    node_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
) -> SkillRunStarted:
    node = await _get_owned_node(db, node_id, current.organization_id)

    if node.type == "source":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Source nodes don't run skills")

    try:
        skill_name = skill_for_node(node)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    skill_run = SkillRun(
        node_id=node.id,
        skill=skill_name,
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    db.add(skill_run)
    await db.flush()

    pool = await get_arq_pool()
    await pool.enqueue_job("run_skill", str(skill_run.id))

    return SkillRunStarted(skill_run_id=skill_run.id, skill=skill_name, status="pending")


@router.post(
    "/canvases/{canvas_id}/run-all",
    response_model=BulkRunStarted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_canvas(
    canvas_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
) -> BulkRunStarted:
    """Enqueue runs for every node in the canvas that has incoming input.

    Order:
        1. extract nodes whose source has content
        2. format nodes whose extract has talking_points + selected_index,
           OR whose source has content (when wired source→format directly)

    The worker processes them via the queue; format nodes will pick up
    fresh extract output via collect_input_for_skill once their parent
    finishes (we DO NOT wait — the second wave will see partial state if
    they read upstream too eagerly). For deterministic chains use the
    UI's per-node Run button or wait for an extract to finish before
    triggering a format. Bulk-run is best for "refresh everything".
    """
    canvas = await db.scalar(
        select(Canvas).where(
            Canvas.id == canvas_id, Canvas.organization_id == current.organization_id
        )
    )
    if canvas is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Canvas not found")

    nodes = list(
        (await db.scalars(select(Node).where(Node.canvas_id == canvas_id))).all()
    )
    edges = list(
        (await db.scalars(select(Edge).where(Edge.canvas_id == canvas_id))).all()
    )

    parent_of: dict[uuid.UUID, Node] = {}
    by_id = {n.id: n for n in nodes}
    for e in edges:
        if e.target_node_id in by_id and e.source_node_id in by_id:
            parent_of[e.target_node_id] = by_id[e.source_node_id]

    pool = await get_arq_pool()
    started: list[SkillRunStarted] = []
    skipped = 0

    # Two waves: extract first, then format. Same canvas → safe ordering.
    waves = [
        [n for n in nodes if n.type == "extract"],
        [n for n in nodes if n.type == "format"],
    ]
    for wave in waves:
        for n in wave:
            parent = parent_of.get(n.id)
            if parent is None:
                skipped += 1
                continue
            parent_data = parent.data or {}
            if n.type == "extract" and not (parent_data.get("content") or "").strip():
                skipped += 1
                continue
            if n.type == "format":
                if parent.type == "extract":
                    tps = parent_data.get("talking_points") or []
                    if not tps or parent_data.get("selected_index") is None:
                        skipped += 1
                        continue
                elif parent.type == "source":
                    if not (parent_data.get("content") or "").strip():
                        skipped += 1
                        continue

            try:
                skill_name = skill_for_node(n)
            except ValueError:
                skipped += 1
                continue

            sr = SkillRun(
                node_id=n.id,
                skill=skill_name,
                status="pending",
                created_at=datetime.now(timezone.utc),
            )
            db.add(sr)
            await db.flush()
            await pool.enqueue_job("run_skill", str(sr.id))
            started.append(
                SkillRunStarted(skill_run_id=sr.id, skill=skill_name, status="pending")
            )

    return BulkRunStarted(skill_runs=started, skipped=skipped)


@router.get("/skill-runs/{skill_run_id}", response_model=SkillRunOut)
async def get_skill_run(
    skill_run_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
) -> SkillRunOut:
    sr = await _get_owned_skill_run(db, skill_run_id, current.organization_id)
    return SkillRunOut.model_validate(sr)


@router.get("/skill-runs/{skill_run_id}/stream")
async def stream_skill_run(
    skill_run_id: uuid.UUID,
    current: CurrentUserSse,
    db: DbSession,
    request: Request,
):
    sr = await _get_owned_skill_run(db, skill_run_id, current.organization_id)

    async def event_gen():
        yield {
            "event": "status",
            "data": json.dumps({"status": sr.status}),
        }
        if sr.status in ("completed", "failed"):
            final_event = "complete" if sr.status == "completed" else "error"
            payload = (
                {"node_id": str(sr.node_id)}
                if sr.status == "completed"
                else {"message": sr.error or "failed"}
            )
            yield {"event": final_event, "data": json.dumps(payload, default=str)}
            return

        try:
            async for evt in events.subscribe(skill_run_id):
                if await request.is_disconnected():
                    break
                yield {
                    "event": evt.get("event", "message"),
                    "data": json.dumps(evt.get("data") or {}, default=str),
                }
                if evt.get("event") in ("complete", "error"):
                    break
        except asyncio.CancelledError:
            raise

    return EventSourceResponse(event_gen())
