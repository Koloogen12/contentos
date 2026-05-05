"""Tiny pub/sub helper over Redis for SSE streaming of skill-run progress.

Channel naming: `skill_run:{skill_run_id}`
Each event is JSON: {"event": "<name>", "data": {...}}.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from redis import asyncio as aioredis

from app.config import settings

_pub: aioredis.Redis | None = None


def _channel(skill_run_id: uuid.UUID) -> str:
    return f"skill_run:{skill_run_id}"


def _redis() -> aioredis.Redis:
    global _pub
    if _pub is None:
        _pub = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _pub


async def publish(skill_run_id: uuid.UUID, event: str, data: dict[str, Any]) -> None:
    payload = json.dumps({"event": event, "data": data}, default=str)
    await _redis().publish(_channel(skill_run_id), payload)


async def subscribe(skill_run_id: uuid.UUID) -> AsyncIterator[dict[str, Any]]:
    """Yields {"event": str, "data": dict} until the caller stops iterating."""
    pubsub = _redis().pubsub()
    await pubsub.subscribe(_channel(skill_run_id))
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                payload = json.loads(message["data"])
            except (TypeError, ValueError):
                continue
            yield payload
    finally:
        await pubsub.unsubscribe(_channel(skill_run_id))
        await pubsub.close()
