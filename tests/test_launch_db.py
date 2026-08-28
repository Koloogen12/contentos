"""Интеграционные тесты модуля прогревов против настоящей базы.

Проверяется то, что нельзя проверить на чистых функциях: что миграция
накатывается, что слоты действительно пишутся, что прогрев не протекает в
обычный контент-план и что подбор идей не ставит одну идею дважды.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.conftest import integration

pytestmark = [pytest.mark.asyncio, integration]

TODAY = date.today()
SALES_OPEN = TODAY + timedelta(days=45)
SALES_CLOSE = SALES_OPEN + timedelta(days=6)


async def _make_launch(session, org, **kw):
    from app.models.launch import Launch

    launch = Launch(
        organization_id=org.id,
        name=kw.pop("name", "Тестовый запуск"),
        sales_open=kw.pop("sales_open", SALES_OPEN),
        sales_close=kw.pop("sales_close", SALES_CLOSE),
        readiness={},
        durations={},
        **kw,
    )
    session.add(launch)
    await session.flush()
    return launch


async def _make_ideas(session, org, meaning: str, count: int, fmt: str = "any"):
    from app.models.knowledge import KnowledgeItem

    out = []
    for i in range(count):
        item = KnowledgeItem(
            organization_id=org.id,
            type="tezis",
            title=f"Идея {meaning} #{i}",
            body="Текст идеи",
            launch_meaning=meaning,
            content_format=fmt,
            markup_origin="rule",
            launch_checkpoints=[],
            launch_triggers=[],
            tags=[],
        )
        session.add(item)
        out.append(item)
    await session.flush()
    return out


# ---------------------------------------------------------------------------
# Схема и миграция
# ---------------------------------------------------------------------------


async def test_launch_row_roundtrips(session, org):
    launch = await _make_launch(session, org)
    assert launch.id is not None
    assert launch.status == "draft"
    assert launch.intensity == "normal"


async def test_sales_window_constraint_enforced(session, org):
    """Закрытие раньше открытия должно отбиваться самой базой."""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await _make_launch(
            session, org, sales_open=SALES_OPEN, sales_close=SALES_OPEN - timedelta(days=1)
        )
    await session.rollback()


async def test_key_event_after_sales_rejected_by_db(session, org):
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await _make_launch(
            session, org, key_event_date=SALES_OPEN + timedelta(days=1)
        )
    await session.rollback()


async def test_stories_platform_allowed_after_migration(session, org):
    """Миграция расширяет список площадок: сторис — отдельный формат."""
    from app.models.content_plan import PlannedPost

    post = PlannedPost(
        organization_id=org.id, platform="stories", status="draft",
        scheduled_date=SALES_OPEN, checkpoints=[], tags=[], metrics={},
    )
    session.add(post)
    await session.flush()
    assert post.id is not None


async def test_markup_origin_constraint(session, org):
    from sqlalchemy.exc import IntegrityError
    from app.models.content_plan import PlannedPost

    post = PlannedPost(
        organization_id=org.id, platform="telegram", status="draft",
        markup_origin="сомнительно", checkpoints=[], tags=[], metrics={},
    )
    session.add(post)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


# ---------------------------------------------------------------------------
# Генерация плана
# ---------------------------------------------------------------------------


async def test_generate_plan_creates_slots(session, org):
    from app.services.launch import service

    launch = await _make_launch(session, org)
    plan, created = await service.generate_plan(session, launch, today=TODAY)

    assert created, "план должен породить слоты"
    assert all(s.launch_id == launch.id for s in created)
    assert all(s.scheduled_date >= TODAY for s in created)
    assert {w.stage for w in plan.windows} <= set(range(1, 8))


async def test_generated_slots_carry_stage_and_meaning(session, org):
    from app.services.launch import service

    launch = await _make_launch(session, org)
    _, created = await service.generate_plan(session, launch, today=TODAY)
    assert all(s.launch_stage for s in created)
    assert all(s.meaning for s in created)
    assert all(s.markup_origin == "rule" for s in created), (
        "свежесозданная разметка обязана быть черновой"
    )


async def test_regenerate_keeps_published_slots(session, org):
    """Пересчёт не имеет права затирать опубликованное."""
    from app.services.launch import service

    launch = await _make_launch(session, org)
    _, created = await service.generate_plan(session, launch, today=TODAY)
    victim = created[0]
    victim.status = "published"
    victim.full_text = "уже вышло"
    await session.flush()
    kept_id = victim.id

    await service.generate_plan(session, launch, today=TODAY)
    slots = await service.load_slots(session, launch.id)
    assert kept_id in {s.id for s in slots}, "опубликованная единица исчезла при пересчёте"


async def test_regenerate_keeps_pinned_slots(session, org):
    from app.services.launch import service

    launch = await _make_launch(session, org)
    _, created = await service.generate_plan(session, launch, today=TODAY)
    pinned = created[1]
    pinned.is_pinned = True
    await session.flush()
    pinned_id = pinned.id

    await service.generate_plan(session, launch, today=TODAY)
    slots = await service.load_slots(session, launch.id)
    assert pinned_id in {s.id for s in slots}, "закреплённая руками правка потеряна"


# ---------------------------------------------------------------------------
# Подбор идей
# ---------------------------------------------------------------------------


async def test_assign_ideas_fills_slots(session, org):
    from app.services.launch import service

    launch = await _make_launch(session, org)
    await service.generate_plan(session, launch, today=TODAY)
    slots = await service.load_slots(session, launch.id)
    meanings = {s.meaning for s in slots if s.meaning}
    for meaning in meanings:
        await _make_ideas(session, org, meaning, 30)

    result = await service.assign_ideas(session, launch)
    assert result["filled"] > 0


async def test_assign_ideas_never_reuses_one_idea(session, org):
    from app.services.launch import service

    launch = await _make_launch(session, org)
    await service.generate_plan(session, launch, today=TODAY)
    slots = await service.load_slots(session, launch.id)
    for meaning in {s.meaning for s in slots if s.meaning}:
        await _make_ideas(session, org, meaning, 30)
    await service.assign_ideas(session, launch)

    slots = await service.load_slots(session, launch.id)
    used = [s.knowledge_item_id for s in slots if s.knowledge_item_id]
    assert len(used) == len(set(used)), "одна идея заняла несколько слотов"


async def test_empty_slot_explains_why(session, org):
    """Пустой слот обязан назвать причину, а не молчать."""
    from app.services.launch import service

    launch = await _make_launch(session, org)
    await service.generate_plan(session, launch, today=TODAY)
    await service.assign_ideas(session, launch)

    slots = await service.load_slots(session, launch.id)
    empty = [s for s in slots if not s.knowledge_item_id]
    assert empty, "при пустом банке слоты обязаны остаться пустыми"
    assert all(s.notes for s in empty), "у пустого слота нет объяснения"


# ---------------------------------------------------------------------------
# Изоляция от обычного контент-плана
# ---------------------------------------------------------------------------


async def test_launch_slots_absent_from_regular_plan(session, org):
    """Главное требование: прогрев не смешивается с обычным планом."""
    from app.services.content_plan import list_posts_in_range
    from app.services.launch import service

    launch = await _make_launch(session, org)
    _, created = await service.generate_plan(session, launch, today=TODAY)
    assert created

    regular = await list_posts_in_range(
        session,
        organization_id=org.id,
        date_from=TODAY,
        date_to=SALES_CLOSE + timedelta(days=5),
    )
    assert all(p.launch_id is None for p in regular), (
        "единицы прогрева протекли в регулярный контент-план"
    )


async def test_regular_post_still_visible(session, org):
    from app.models.content_plan import PlannedPost
    from app.services.content_plan import list_posts_in_range

    post = PlannedPost(
        organization_id=org.id, platform="telegram", status="draft",
        scheduled_date=TODAY, checkpoints=[], tags=[], metrics={},
    )
    session.add(post)
    await session.flush()

    got = await list_posts_in_range(
        session, organization_id=org.id, date_from=TODAY, date_to=TODAY
    )
    assert post.id in {p.id for p in got}


# ---------------------------------------------------------------------------
# Разметка банка и отчёт
# ---------------------------------------------------------------------------


async def test_markup_bank_marks_as_draft(session, org):
    from app.services.launch import service

    await _make_ideas(session, org, "hype", 3)
    from app.models.knowledge import KnowledgeItem
    from sqlalchemy import select

    rows = await session.scalars(
        select(KnowledgeItem).where(KnowledgeItem.organization_id == org.id)
    )
    for item in rows.all():
        item.launch_meaning = None
        item.markup_origin = None
    await session.flush()

    result = await service.markup_bank(session, org.id)
    assert result["marked"] >= 0
    rows = await session.scalars(
        select(KnowledgeItem).where(KnowledgeItem.organization_id == org.id)
    )
    assert all(
        i.markup_origin == "rule" for i in rows.all()
    ), "автоматическая разметка обязана быть помечена как черновая"


async def test_report_on_fresh_launch_is_not_ready(session, org):
    from app.services.launch import service

    launch = await _make_launch(session, org)
    await service.generate_plan(session, launch, today=TODAY)
    report = await service.build_launch_report(session, launch)

    assert not report.ready, "пустой прогрев не может считаться готовым"
    assert report.slots_total > 0
    assert report.checkpoints_confirmed == 0


async def test_report_flags_unready_product(session, org):
    from app.services.launch import service

    launch = await _make_launch(session, org)
    launch.readiness = {"program": True, "payments": False}
    await session.flush()
    await service.generate_plan(session, launch, today=TODAY)
    report = await service.build_launch_report(session, launch)

    codes = {f.code for f in report.findings}
    assert "product_not_ready" in codes


# ---------------------------------------------------------------------------
# Защита от потери работы
# ---------------------------------------------------------------------------


async def test_preview_org_with_launch_survives_cleanup(session, org):
    """Песочница с запуском не должна вычищаться по таймеру.

    Прогрев строят неделями и заходят в него не каждый день. Снести
    организацию по сроку бездействия значит стереть работу человека
    прямо перед запуском — это худший возможный первый опыт.
    """
    from datetime import datetime, timedelta as td, timezone as tz

    from sqlalchemy import select

    from app.models.auth import Organization
    from app.models.launch import Launch
    from app.services.trial import PREVIEW_CLEANUP_DAYS

    org.kind = "preview"
    org.created_at = datetime.now(tz.utc) - td(days=PREVIEW_CLEANUP_DAYS + 5)
    await _make_launch(session, org)
    await session.flush()

    cutoff = datetime.now(tz.utc) - td(days=PREVIEW_CLEANUP_DAYS)
    candidates = list(
        (
            await session.scalars(
                select(Organization.id).where(
                    Organization.kind == "preview",
                    Organization.created_at < cutoff,
                    ~select(Launch.id)
                    .where(Launch.organization_id == Organization.id)
                    .exists(),
                )
            )
        ).all()
    )
    assert org.id not in candidates, "организацию с запуском собрались удалить"


async def test_preview_org_without_launch_is_still_collected(session, org):
    """Обратная сторона: пустая брошенная песочница по-прежнему убирается."""
    from datetime import datetime, timedelta as td, timezone as tz

    from sqlalchemy import select

    from app.models.auth import Organization
    from app.models.launch import Launch
    from app.services.trial import PREVIEW_CLEANUP_DAYS

    org.kind = "preview"
    org.created_at = datetime.now(tz.utc) - td(days=PREVIEW_CLEANUP_DAYS + 5)
    await session.flush()

    cutoff = datetime.now(tz.utc) - td(days=PREVIEW_CLEANUP_DAYS)
    candidates = list(
        (
            await session.scalars(
                select(Organization.id).where(
                    Organization.kind == "preview",
                    Organization.created_at < cutoff,
                    ~select(Launch.id)
                    .where(Launch.organization_id == Organization.id)
                    .exists(),
                )
            )
        ).all()
    )
    assert org.id in candidates


async def test_reference_route_not_shadowed_by_launch_id(session, org):
    """`/launches/reference` не должен приниматься за идентификатор запуска."""
    from fastapi import FastAPI

    from app.api.v1 import launches as launches_api

    app = FastAPI()
    app.include_router(launches_api.router, prefix="/api/v1")
    paths = [r.path for r in app.routes if hasattr(r, "path")]
    ref = paths.index("/api/v1/launches/reference")
    by_id = paths.index("/api/v1/launches/{launch_id}")
    assert ref < by_id, "статичный маршрут обязан быть объявлен раньше параметра"
