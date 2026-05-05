"""Skill: pull a YouTube transcript (captions or whisper) into a source node."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Node
from app.services.skills.base import register
from app.services.transcription.youtube import transcribe


@register("transcribe_youtube")
async def run(
    db: AsyncSession,
    node: Node,
    system_context: str,
    skill_input: dict[str, Any],
) -> dict[str, Any]:
    url = (skill_input.get("url") or "").strip()
    if not url:
        raise ValueError("URL не передан")

    result = await transcribe(url)

    new_data = dict(node.data or {})
    new_data.update(
        {
            "input_type": "youtube",
            "content": result["content"],
            "youtube_url": result.get("youtube_url"),
            "youtube_video_id": result.get("youtube_video_id"),
            "youtube_title": result.get("youtube_title"),
            "youtube_duration_seconds": result.get("youtube_duration_seconds"),
            "transcript_method": result.get("transcript_method"),
            "transcript_language": result.get("transcript_language"),
        }
    )
    return {
        "node_data": new_data,
        "meta": {
            "method": result.get("transcript_method"),
            "chars": len(result["content"]),
        },
    }
