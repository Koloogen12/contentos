"""Общее окружение интеграционных тестов.

Тесты гоняются против настоящего Postgres: у модуля прогревов
postgres-специфичная схема (массивы, JSONB, gen_random_uuid), и sqlite
её не воспроизводит — проверять на другом движке значит проверять не то,
что поедет в прод.

Приложение целиком не поднимаем: собираем мини-приложение из одного
роутера запусков. Полный `app.main` тянет playwright, aiogram и yt-dlp,
которые к прогреву отношения не имеют.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production")

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://contentos:contentos@localhost:5432/contentos_test",
)
HAS_DB = bool(os.environ.get("DATABASE_URL"))

integration = pytest.mark.skipif(
    not HAS_DB, reason="нужен Postgres: запускать через docker compose -f docker-compose.test.yml"
)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session", autouse=True)
def migrated() -> None:
    """Накатить все миграции с нуля.

    Прогоняем весь путь 0001→0017, а не только последнюю: миграция модуля
    меняет ограничения существующих таблиц, и её нужно проверять в
    реальной последовательности.
    """
    if not HAS_DB:
        return
    subprocess.run(["alembic", "upgrade", "head"], check=True)


@pytest_asyncio.fixture
async def session() -> AsyncIterator:
    if not HAS_DB:
        pytest.skip("нет базы")
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(DB_URL, poolclass=None)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def org(session):
    """Организация с пользователем — минимальный арендатор для тестов."""
    from app.models.auth import Organization, User

    suffix = uuid.uuid4().hex[:8]
    organization = Organization(name=f"Test {suffix}", slug=f"test-{suffix}")
    session.add(organization)
    await session.flush()
    user = User(
        organization_id=organization.id,
        email=f"t-{suffix}@example.com",
        password_hash="x",
        display_name="Тест",
    )
    session.add(user)
    await session.flush()
    return organization
