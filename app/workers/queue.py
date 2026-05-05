"""Arq worker entry."""
from __future__ import annotations

from arq.connections import RedisSettings, create_pool

from app.config import settings
from app.workers.tasks import run_skill

_pool = None


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.REDIS_URL)


async def get_arq_pool():
    """Lazily-created Arq Redis pool used by API endpoints to enqueue jobs."""
    global _pool
    if _pool is None:
        _pool = await create_pool(_redis_settings())
    return _pool


async def startup(ctx: dict) -> None:
    pass


async def shutdown(ctx: dict) -> None:
    pass


class WorkerSettings:
    functions = [run_skill]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _redis_settings()
    max_jobs = 10
    job_timeout = 300
