"""Transcription endpoints for source nodes: YouTube + audio upload."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.canvas import Canvas, Node, SkillRun
from app.schemas.transcription import (
    TranscribeYoutubeIn,
    TranscriptionStarted,
    YoutubeMetaOut,
)
from app.services import storage
from app.services.transcription.youtube import fetch_meta
from app.workers.queue import get_arq_pool

router = APIRouter(tags=["transcription"])


async def _owned_source_node(db, node_id: uuid.UUID, org_id: uuid.UUID) -> Node:
    node = await db.scalar(
        select(Node).join(Canvas).where(Node.id == node_id, Canvas.organization_id == org_id)
    )
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Node not found")
    if node.type != "source":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only source nodes can be transcribed")
    return node


async def _enqueue_skill_with_input(
    db,
    *,
    node_id: uuid.UUID,
    skill: str,
    input_snapshot: dict,
) -> SkillRun:
    sr = SkillRun(
        node_id=node_id,
        skill=skill,
        status="pending",
        created_at=datetime.now(timezone.utc),
        input_snapshot=input_snapshot,
    )
    db.add(sr)
    await db.flush()

    pool = await get_arq_pool()
    await pool.enqueue_job("run_skill", str(sr.id))
    return sr


@router.get("/nodes/{node_id}/youtube-meta", response_model=YoutubeMetaOut)
async def youtube_meta(
    node_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
    url: str = Query(..., min_length=1),
) -> YoutubeMetaOut:
    await _owned_source_node(db, node_id, current.organization_id)
    try:
        meta = await fetch_meta(url)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Не удалось получить мета: {exc}") from exc
    return YoutubeMetaOut(
        title=meta.get("title"),
        duration_seconds=meta.get("duration_seconds"),
        channel=meta.get("channel"),
        video_id=meta.get("video_id"),
    )


@router.post(
    "/nodes/{node_id}/transcribe-youtube",
    response_model=TranscriptionStarted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def transcribe_youtube(
    node_id: uuid.UUID,
    payload: TranscribeYoutubeIn,
    current: CurrentUser,
    db: DbSession,
) -> TranscriptionStarted:
    await _owned_source_node(db, node_id, current.organization_id)
    sr = await _enqueue_skill_with_input(
        db,
        node_id=node_id,
        skill="transcribe_youtube",
        input_snapshot={"url": payload.url},
    )
    return TranscriptionStarted(skill_run_id=sr.id, skill="transcribe_youtube", status="pending")


@router.post(
    "/nodes/{node_id}/upload-audio",
    response_model=TranscriptionStarted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_audio(
    node_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
    file: UploadFile = File(...),
) -> TranscriptionStarted:
    await _owned_source_node(db, node_id, current.organization_id)

    storage_path = storage.save_upload(file.file, file.filename or "upload.bin")

    sr = await _enqueue_skill_with_input(
        db,
        node_id=node_id,
        skill="transcribe_audio",
        input_snapshot={
            "storage_path": storage_path,
            "file_name": file.filename,
            "file_size_bytes": file.size,
            "file_type": file.content_type,
        },
    )
    return TranscriptionStarted(skill_run_id=sr.id, skill="transcribe_audio", status="pending")
