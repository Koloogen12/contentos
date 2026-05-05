"""Skill: transcribe an uploaded audio file via whisper."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Node
from app.services import storage
from app.services.skills.base import register
from app.services.transcription.audio import transcribe


@register("transcribe_audio")
async def run(
    db: AsyncSession,
    node: Node,
    system_context: str,
    skill_input: dict[str, Any],
) -> dict[str, Any]:
    storage_path = skill_input.get("storage_path")
    if not storage_path:
        raise ValueError("Файл не загружен")

    local = storage.resolve_to_local(storage_path)
    try:
        text = await transcribe(local)
    finally:
        # Local-scheme uploads are owned by us; clean them up after.
        # S3 uploads stay — caller may want to re-process.
        if storage_path.startswith("local://"):
            storage.cleanup(storage_path)

    new_data = dict(node.data or {})
    new_data.update(
        {
            "input_type": "file_upload",
            "content": text,
            "file_name": skill_input.get("file_name"),
            "file_size_bytes": skill_input.get("file_size_bytes"),
            "file_type": skill_input.get("file_type"),
            "transcript_method": "whisper",
            "transcript_language": skill_input.get("language") or "auto",
        }
    )
    return {"node_data": new_data, "meta": {"chars": len(text)}}
