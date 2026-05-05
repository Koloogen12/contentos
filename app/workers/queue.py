"""Arq worker entry. Tasks are stubs for now — populated in next iteration."""
from arq.connections import RedisSettings

from app.config import settings


async def noop(ctx: dict) -> str:
    return "ok"


async def startup(ctx: dict) -> None:
    pass


async def shutdown(ctx: dict) -> None:
    pass


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.REDIS_URL)


class WorkerSettings:
    functions = [noop]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _redis_settings()
