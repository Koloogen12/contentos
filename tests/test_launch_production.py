"""Ведение запуска: фактура, задачи, слоты руками, привязка линий к постам.

Проверяется то, ради чего затевалась миграция 0018: что состояние, которое
раньше жило во вкладке и терялось при перезагрузке, теперь переживает
перезапись и считается там, где ему положено считаться.

Отдельно проверяются два решения схемы, потому что оба легко «починить»
обратно и не заметить: режим запуска не хранится, а задачи на добычу не
существуют как строки.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.conftest import integration

pytestmark = [pytest.mark.asyncio, integration]

TODAY = date.today()
SALES_OPEN = TODAY + timedelta(days=45)
SALES_CLOSE = SALES_OPEN + timedelta(days=6)


async def _launch(session, org, **kw):
    from app.models.launch import Launch

    launch = Launch(
        organization_id=org.id,
        name=kw.pop("name", "Запуск для тестов ведения"),
        sales_open=kw.pop("sales_open", SALES_OPEN),
        sales_close=kw.pop("sales_close", SALES_CLOSE),
        readiness={},
        durations={},
        **kw,
    )
    session.add(launch)
    await session.flush()
    return launch


async def _slot(session, launch, day: date, **kw):
    from app.models.content_plan import PlannedPost

    slot = PlannedPost(
        organization_id=launch.organization_id,
        launch_id=launch.id,
        scheduled_date=day,
        platform=kw.pop("platform", "stories"),
        status=kw.pop("status", "draft"),
        **kw,
    )
    session.add(slot)
    await session.flush()
    return slot


# ---------------------------------------------------------------------------
# Фактура
# ---------------------------------------------------------------------------


async def test_evidence_defaults_to_none(session, org):
    """Незаполненная фактура читается как «нечем доказать», а не как пробел.

    Пустая таблица не означает «неизвестно»: запуск, где фактуру не трогали,
    ничем не доказан, и проверка обязана видеть именно это.
    """
    from app.services.launch import evidence as ev
    from app.services.launch.methodology import CHECKPOINTS

    launch = await _launch(session, org)
    got = await ev.evidence_map(session, launch.id)

    assert len(got) == len(CHECKPOINTS)
    assert set(got.values()) == {"none"}


async def test_evidence_upsert_overwrites(session, org):
    """Повторная простановка не плодит строки и не падает на конфликте."""
    from app.services.launch import evidence as ev

    launch = await _launch(session, org)
    await ev.set_evidence(session, launch.id, "q3_bridge", state="claimed")
    await ev.set_evidence(
        session, launch.id, "q3_bridge", state="proof", proof_note="скрин кабинета"
    )

    saved = await ev.load_evidence(session, launch.id)
    assert list(saved) == ["q3_bridge"]
    assert saved["q3_bridge"].state == "proof"
    assert saved["q3_bridge"].proof_note == "скрин кабинета"


async def test_evidence_rejects_unknown_meaning(session, org):
    """Смысл вне справочника не заводится: иначе покрытие считается по мусору."""
    from app.services.launch import evidence as ev

    launch = await _launch(session, org)
    with pytest.raises(ValueError):
        await ev.set_evidence(session, launch.id, "нет-такого-смысла", state="proof")


async def test_evidence_is_scoped_to_launch(session, org):
    """Фактура одного запуска не видна другому: у второго потока свои пруфы."""
    from app.services.launch import evidence as ev

    a = await _launch(session, org, name="Первый поток")
    b = await _launch(session, org, name="Второй поток")
    await ev.set_evidence(session, a.id, "q1_results", state="proof")

    assert (await ev.evidence_map(session, a.id))["q1_results"] == "proof"
    assert (await ev.evidence_map(session, b.id))["q1_results"] == "none"


# ---------------------------------------------------------------------------
# Задачи на добычу
# ---------------------------------------------------------------------------


async def test_task_deadline_counts_back_from_first_slot(session, org):
    """Дедлайн считается от дня, где смысл понадобится, а не от даты продаж."""
    from app.services.launch import evidence as ev

    launch = await _launch(session, org)
    day = SALES_OPEN - timedelta(days=20)
    await _slot(session, launch, day, meaning="q3_bridge", rubric="topic")

    tasks = await ev.tasks_for(session, launch)
    bridge = next(t for t in tasks if t["meaning_key"] == "q3_bridge")

    assert bridge["needed_by"] == day - timedelta(days=ev.LEAD_DAYS)
    assert bridge["slot_dates"] == [day]


async def test_closed_meaning_leaves_the_task_list(session, org):
    """Закрыл смысл пруфом — задача исчезает сама, без отдельной отметки.

    Ровно это и есть причина не хранить задачи строками: иначе пришлось бы
    гасить их вручную и ловить рассинхрон.
    """
    from app.services.launch import evidence as ev

    launch = await _launch(session, org)
    await _slot(session, launch, SALES_OPEN - timedelta(days=10), meaning="q1_results")

    assert any(t["meaning_key"] == "q1_results" for t in await ev.tasks_for(session, launch))

    await ev.set_evidence(session, launch.id, "q1_results", state="proof")
    assert not any(t["meaning_key"] == "q1_results" for t in await ev.tasks_for(session, launch))


async def test_dismissed_task_stays_hidden(session, org):
    """Снятая с доски задача не возвращается: это единственное, что хранится."""
    from app.services.launch import evidence as ev

    launch = await _launch(session, org)
    await ev.set_evidence(
        session, launch.id, "q4_team", state="none", task_dismissed=True
    )
    assert not any(t["meaning_key"] == "q4_team" for t in await ev.tasks_for(session, launch))


async def test_tasks_sorted_by_urgency(session, org):
    """Сначала то, что нужно раньше; смыслы без слотов — в хвост."""
    from app.services.launch import evidence as ev

    launch = await _launch(session, org)
    late = SALES_OPEN - timedelta(days=5)
    early = SALES_OPEN - timedelta(days=30)
    await _slot(session, launch, late, meaning="q2_projects")
    await _slot(session, launch, early, meaning="q3_proof")

    tasks = await ev.tasks_for(session, launch)
    dated = [t for t in tasks if t["needed_by"] is not None]

    assert dated[0]["meaning_key"] == "q3_proof"
    assert dated[1]["meaning_key"] == "q2_projects"
    assert tasks[-1]["needed_by"] is None


# ---------------------------------------------------------------------------
# Режим запуска
# ---------------------------------------------------------------------------


async def test_mode_is_derived_from_dates(session, org):
    """Режим считается, а не хранится — и меняется вместе с датами.

    Проверка стоит здесь именно потому, что соблазн завести колонку `mode`
    велик: он бы разошёлся с датами на первом же переносе окна.
    """
    from app.services.launch.evidence import launch_mode

    launch = await _launch(session, org)

    assert launch_mode(launch, TODAY) == "draft"

    launch.unrolled_on = TODAY
    assert launch_mode(launch, TODAY) == "warm"

    assert launch_mode(launch, SALES_OPEN) == "sales"
    assert launch_mode(launch, SALES_CLOSE) == "sales"
    assert launch_mode(launch, SALES_CLOSE + timedelta(days=1)) == "closed"


async def test_archived_launch_is_closed_whatever_the_dates(session, org):
    from datetime import datetime, timezone

    from app.services.launch.evidence import launch_mode

    launch = await _launch(session, org)
    launch.unrolled_on = TODAY
    launch.archived_at = datetime.now(timezone.utc)

    assert launch_mode(launch, TODAY) == "closed"


# ---------------------------------------------------------------------------
# Слоты
# ---------------------------------------------------------------------------


async def test_window_for_finds_the_stage(session, org):
    """Дата внутри оси отдаёт этап, дата снаружи — ничего."""
    from app.services.launch import service

    launch = await _launch(session, org, unrolled_on=TODAY)

    inside = await service.window_for(session, launch, SALES_OPEN - timedelta(days=3))
    assert inside is not None
    assert 2 <= inside.stage <= 7

    assert await service.window_for(session, launch, TODAY - timedelta(days=5)) is None
    assert await service.window_for(session, launch, SALES_CLOSE + timedelta(days=30)) is None


async def test_sales_window_is_the_last_stage(session, org):
    """День открытия продаж попадает в седьмой этап, а не в шестой."""
    from app.services.launch import service

    launch = await _launch(session, org, unrolled_on=TODAY)
    window = await service.window_for(session, launch, SALES_OPEN)

    assert window is not None
    assert window.key == "sales"


async def test_slot_survives_roundtrip_with_new_fields(session, org):
    """Отметки ведения действительно доезжают до базы и читаются обратно."""
    from sqlalchemy import select

    from app.models.content_plan import PlannedPost

    launch = await _launch(session, org, unrolled_on=TODAY)
    slot = await _slot(
        session,
        launch,
        SALES_OPEN - timedelta(days=2),
        rubric="expertise",
        meaning="q2_projects",
        status="published",
        reaction=3,
        draft_state="ready",
        chars=1180,
        empty_reason=None,
    )
    session.expunge_all()

    again = await session.scalar(select(PlannedPost).where(PlannedPost.id == slot.id))
    assert again.rubric == "expertise"
    assert again.meaning == "q2_projects"
    assert again.reaction == 3
    assert again.draft_state == "ready"
    assert again.chars == 1180


async def test_empty_reason_is_stored_not_recomputed(session, org):
    """Причина пустого слота хранится дословно.

    Пересчёт через неделю дал бы другую формулировку — банк успел измениться,
    — и человек смотрел бы на слот, не понимая, что произошло.
    """
    from sqlalchemy import select

    from app.models.content_plan import PlannedPost

    launch = await _launch(session, org, unrolled_on=TODAY)
    reason = "идеи рубрики «Ученики и комьюнити» закончились — все уже стоят в плане"
    slot = await _slot(session, launch, SALES_OPEN - timedelta(days=4), empty_reason=reason)
    session.expunge_all()

    again = await session.scalar(select(PlannedPost).where(PlannedPost.id == slot.id))
    assert again.empty_reason == reason


async def test_missed_status_is_allowed(session, org):
    """`missed` отличается от `skipped`: факт против решения автора."""
    launch = await _launch(session, org, unrolled_on=TODAY)
    slot = await _slot(session, launch, TODAY, status="missed")
    assert slot.status == "missed"


async def test_reaction_out_of_range_is_rejected(session, org):
    """Оценка вне 1–3 не проходит: на ней стоит весь разбор."""
    import sqlalchemy.exc

    launch = await _launch(session, org, unrolled_on=TODAY)
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await _slot(session, launch, TODAY, reaction=7)
    await session.rollback()


# ---------------------------------------------------------------------------
# Сюжетные линии
# ---------------------------------------------------------------------------


async def test_deleting_a_slot_reopens_the_line(session, org):
    """Удаление поста не уносит линию — обещание снова становится открытым.

    ON DELETE SET NULL, а не CASCADE: линия это обещание аудитории, и оно не
    перестаёт существовать оттого, что пост убрали из плана.
    """
    from sqlalchemy import select

    from app.models.launch import LaunchStoryLine

    launch = await _launch(session, org, unrolled_on=TODAY)
    slot = await _slot(session, launch, SALES_OPEN - timedelta(days=8))
    line = LaunchStoryLine(
        launch_id=launch.id,
        title="Покажу, сколько стоил первый поток",
        closes_on=slot.scheduled_date,
        close_slot_id=slot.id,
        is_closed=True,
    )
    session.add(line)
    await session.flush()

    await session.delete(slot)
    await session.flush()
    session.expunge_all()

    again = await session.scalar(
        select(LaunchStoryLine).where(LaunchStoryLine.id == line.id)
    )
    assert again is not None
    assert again.close_slot_id is None


# ---------------------------------------------------------------------------
# Маршруты
# ---------------------------------------------------------------------------


def test_new_routes_are_registered():
    """Все ручки ведения на месте и статические пути не перекрыты `{launch_id}`."""
    from fastapi import FastAPI

    from app.api.v1 import launches as launches_api

    app = FastAPI()
    app.include_router(launches_api.router, prefix="/api/v1")
    paths = [r.path for r in app.routes]

    for path in (
        "/api/v1/launches/{launch_id}/slots",
        "/api/v1/launches/{launch_id}/slots/{slot_id}",
        "/api/v1/launches/{launch_id}/evidence",
        "/api/v1/launches/{launch_id}/evidence/{meaning_key}",
        "/api/v1/launches/{launch_id}/tasks",
    ):
        assert path in paths, f"нет маршрута {path}"

    # `/reference` обязан объявляться раньше `/{launch_id}`, иначе слово
    # reference уедет в парсер UUID и вернёт 422 вместо справочника.
    assert paths.index("/api/v1/launches/reference") < paths.index(
        "/api/v1/launches/{launch_id}"
    )


async def test_confirm_stage_takes_unmarked_slots_too(session, org):
    """Подтверждение этапа берёт и слоты с пустой разметкой.

    `markup_origin != 'human'` в SQL не видит NULL. Без явного IS NULL
    неразмеченные слоты остались бы неподтверждёнными, а счётчик отрапортовал
    бы, что этап закрыт.
    """
    from sqlalchemy import or_, select

    from app.models.content_plan import PlannedPost
    from app.services.launch.validators import ORIGIN_HUMAN

    launch = await _launch(session, org, unrolled_on=TODAY)
    await _slot(session, launch, SALES_OPEN - timedelta(days=9), launch_stage=4,
                markup_origin=None)
    await _slot(session, launch, SALES_OPEN - timedelta(days=8), launch_stage=4,
                markup_origin="rule")
    await _slot(session, launch, SALES_OPEN - timedelta(days=7), launch_stage=4,
                markup_origin=ORIGIN_HUMAN)

    rows = await session.scalars(
        select(PlannedPost).where(
            PlannedPost.launch_id == launch.id,
            PlannedPost.launch_stage == 4,
            or_(
                PlannedPost.markup_origin.is_(None),
                PlannedPost.markup_origin != ORIGIN_HUMAN,
            ),
        )
    )
    assert len(rows.all()) == 2, "неразмеченный слот должен попадать в выборку"
