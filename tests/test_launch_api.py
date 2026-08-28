"""Ручки ведения запуска через HTTP.

Сервисный слой уже проверен отдельно; здесь проверяется то, что видно только
на границе: коды ответов, форма тела и поведение при конфликте версий. Фронт
опирается ровно на это, и молчаливое расхождение здесь дороже любой ошибки
внутри.

Аутентификация подменяется: она не предмет этих тестов, а поднимать полный
`app.main` ради неё нельзя — в тестовом образе нет тяжёлых зависимостей.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import integration

pytestmark = [pytest.mark.asyncio, integration]

TODAY = date.today()
SALES_OPEN = TODAY + timedelta(days=45)
SALES_CLOSE = SALES_OPEN + timedelta(days=6)


@pytest_asyncio.fixture
async def api(session, org):
    """Роутер запусков с подменённой авторизацией и живой сессией."""
    from fastapi import FastAPI

    from app.api.deps import get_current_user, get_db
    from app.api.v1 import launches as launches_api
    from app.models.auth import User

    user = await session.scalar(
        __import__("sqlalchemy").select(User).where(User.organization_id == org.id)
    )

    app = FastAPI()
    app.include_router(launches_api.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: user

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as client:
        yield client


async def _launch(session, org, **kw):
    from app.models.launch import Launch

    launch = Launch(
        organization_id=org.id,
        name=kw.pop("name", "Запуск для HTTP-тестов"),
        sales_open=kw.pop("sales_open", SALES_OPEN),
        sales_close=kw.pop("sales_close", SALES_CLOSE),
        unrolled_on=kw.pop("unrolled_on", TODAY),
        readiness={},
        durations={},
        **kw,
    )
    session.add(launch)
    await session.flush()
    return launch


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------


async def test_launch_carries_computed_mode(api, session, org):
    """Режим приходит посчитанным — клиент его не выводит заново."""
    launch = await _launch(session, org)
    r = await api.get(f"/api/v1/launches/{launch.id}")

    assert r.status_code == 200
    assert r.json()["mode"] == "warm"
    assert "unrolled_on" in r.json()


async def test_draft_launch_reports_draft_mode(api, session, org):
    launch = await _launch(session, org, unrolled_on=None)
    r = await api.get(f"/api/v1/launches/{launch.id}")
    assert r.json()["mode"] == "draft"


# ---------------------------------------------------------------------------
# Слоты
# ---------------------------------------------------------------------------


async def test_create_slot_pins_it(api, session, org):
    """Слот, добавленный руками, сразу закреплён: пересборка его не смоет."""
    launch = await _launch(session, org)
    r = await api.post(
        f"/api/v1/launches/{launch.id}/slots",
        json={
            "scheduled_date": (SALES_OPEN - timedelta(days=5)).isoformat(),
            "platform": "stories",
            "rubric": "hype",
        },
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["is_pinned"] is True
    assert body["rubric"] == "hype"
    assert body["launch_stage"] is not None


async def test_create_slot_outside_the_axis_is_refused(api, session, org):
    """Дата вне оси — 400 с объяснением, а не слот-призрак."""
    launch = await _launch(session, org)
    r = await api.post(
        f"/api/v1/launches/{launch.id}/slots",
        json={
            "scheduled_date": (SALES_CLOSE + timedelta(days=30)).isoformat(),
            "platform": "stories",
        },
    )
    assert r.status_code == 400
    assert "оси" in r.json()["detail"]


async def test_version_conflict_returns_409(api, session, org):
    """Правка с устаревшей версией не проходит.

    Это главное, ради чего версия вообще есть: слот открывают из двух вкладок,
    и вторая не должна молча стереть первую.
    """
    launch = await _launch(session, org)
    created = (await api.post(
        f"/api/v1/launches/{launch.id}/slots",
        json={
            "scheduled_date": (SALES_OPEN - timedelta(days=4)).isoformat(),
            "platform": "telegram",
        },
    )).json()

    first = await api.patch(
        f"/api/v1/launches/{launch.id}/slots/{created['id']}",
        json={"is_pinned": False, "version": created["version"]},
    )
    assert first.status_code == 200
    assert first.json()["version"] == created["version"] + 1

    stale = await api.patch(
        f"/api/v1/launches/{launch.id}/slots/{created['id']}",
        json={"is_pinned": True, "version": created["version"]},
    )
    assert stale.status_code == 409
    assert "другом окне" in stale.json()["detail"]


async def test_moving_a_slot_moves_its_stage(api, session, org):
    """Перенос меняет этап вместе с датой, иначе слот повиснет между этапами."""
    launch = await _launch(session, org)
    created = (await api.post(
        f"/api/v1/launches/{launch.id}/slots",
        json={
            "scheduled_date": (SALES_OPEN - timedelta(days=30)).isoformat(),
            "platform": "stories",
        },
    )).json()

    moved = await api.patch(
        f"/api/v1/launches/{launch.id}/slots/{created['id']}",
        json={"scheduled_date": SALES_OPEN.isoformat(), "version": created["version"]},
    )
    assert moved.status_code == 200
    # День открытия продаж — седьмой этап.
    assert moved.json()["launch_stage"] == 7


async def test_published_slot_is_not_deleted(api, session, org):
    """Опубликованный слот не удаляется: на нём стоит разбор."""
    launch = await _launch(session, org)
    created = (await api.post(
        f"/api/v1/launches/{launch.id}/slots",
        json={
            "scheduled_date": (SALES_OPEN - timedelta(days=3)).isoformat(),
            "platform": "reels",
        },
    )).json()
    await api.patch(
        f"/api/v1/launches/{launch.id}/slots/{created['id']}",
        json={"status": "published", "version": created["version"]},
    )

    r = await api.delete(f"/api/v1/launches/{launch.id}/slots/{created['id']}")
    assert r.status_code == 409
    assert "разбор" in r.json()["detail"]


async def test_planned_slot_is_deleted(api, session, org):
    launch = await _launch(session, org)
    created = (await api.post(
        f"/api/v1/launches/{launch.id}/slots",
        json={
            "scheduled_date": (SALES_OPEN - timedelta(days=6)).isoformat(),
            "platform": "reels",
        },
    )).json()

    r = await api.delete(f"/api/v1/launches/{launch.id}/slots/{created['id']}")
    assert r.status_code == 204


async def test_confirm_stage_counts_what_it_changed(api, session, org):
    """Подтверждение этапа отдаёт число подтверждённых, а не «ок»."""
    launch = await _launch(session, org)
    day = SALES_OPEN - timedelta(days=2)
    for platform in ("stories", "reels"):
        await api.post(
            f"/api/v1/launches/{launch.id}/slots",
            json={"scheduled_date": day.isoformat(), "platform": platform},
        )
    # Созданные руками уже подтверждены — подтверждать нечего.
    r = await api.post(f"/api/v1/launches/{launch.id}/slots/confirm", json={"stage": 6})
    assert r.status_code == 200
    assert r.json()["confirmed"] == 0


# ---------------------------------------------------------------------------
# Фактура и задачи
# ---------------------------------------------------------------------------


async def test_evidence_returns_all_forty(api, session, org):
    """Отдаются все сорок смыслов, а не только сохранённые."""
    from app.services.launch.methodology import CHECKPOINTS

    launch = await _launch(session, org)
    r = await api.get(f"/api/v1/launches/{launch.id}/evidence")

    assert r.status_code == 200
    body = r.json()
    assert len(body) == len(CHECKPOINTS)
    assert {row["state"] for row in body} == {"none"}


async def test_evidence_patch_roundtrip(api, session, org):
    launch = await _launch(session, org)
    r = await api.patch(
        f"/api/v1/launches/{launch.id}/evidence/q3_bridge",
        json={"state": "proof", "proof_note": "история провала 2023"},
    )
    assert r.status_code == 200
    assert r.json()["state"] == "proof"

    again = await api.get(f"/api/v1/launches/{launch.id}/evidence")
    row = next(x for x in again.json() if x["meaning_key"] == "q3_bridge")
    assert row["proof_note"] == "история провала 2023"


async def test_unknown_meaning_is_404(api, session, org):
    launch = await _launch(session, org)
    r = await api.patch(
        f"/api/v1/launches/{launch.id}/evidence/нет-такого",
        json={"state": "proof"},
    )
    assert r.status_code == 404


async def test_tasks_drop_the_closed_meaning(api, session, org):
    """Закрыл пруфом — задача ушла из выдачи сама."""
    launch = await _launch(session, org)
    before = (await api.get(f"/api/v1/launches/{launch.id}/tasks")).json()
    assert any(t["meaning_key"] == "q1_story" for t in before)

    await api.patch(
        f"/api/v1/launches/{launch.id}/evidence/q1_story", json={"state": "proof"}
    )
    after = (await api.get(f"/api/v1/launches/{launch.id}/tasks")).json()
    assert not any(t["meaning_key"] == "q1_story" for t in after)


# ---------------------------------------------------------------------------
# Сюжетные линии
# ---------------------------------------------------------------------------


async def test_line_closes_only_with_a_slot(api, session, org):
    """`is_closed` — следствие привязки к посту, а не поле ввода."""
    launch = await _launch(session, org)
    line = (await api.post(
        f"/api/v1/launches/{launch.id}/story-lines",
        json={"title": "Покажу, сколько стоил первый поток"},
    )).json()
    assert line["is_closed"] is False

    slot = (await api.post(
        f"/api/v1/launches/{launch.id}/slots",
        json={
            "scheduled_date": (SALES_OPEN - timedelta(days=7)).isoformat(),
            "platform": "telegram",
        },
    )).json()

    linked = await api.patch(
        f"/api/v1/launches/{launch.id}/story-lines/{line['id']}",
        json={"close_slot_id": slot["id"]},
    )
    assert linked.status_code == 200
    body = linked.json()
    assert body["close_slot_id"] == slot["id"]
    assert body["closes_on"] == slot["scheduled_date"]
    assert body["is_closed"] is True


async def test_line_rejects_a_foreign_slot(api, session, org):
    """Слот чужого запуска в линию не привяжешь."""
    a = await _launch(session, org, name="Первый")
    b = await _launch(session, org, name="Второй")
    line = (await api.post(
        f"/api/v1/launches/{a.id}/story-lines", json={"title": "Обещание"}
    )).json()
    foreign = (await api.post(
        f"/api/v1/launches/{b.id}/slots",
        json={
            "scheduled_date": (SALES_OPEN - timedelta(days=7)).isoformat(),
            "platform": "telegram",
        },
    )).json()

    r = await api.patch(
        f"/api/v1/launches/{a.id}/story-lines/{line['id']}",
        json={"close_slot_id": foreign["id"]},
    )
    assert r.status_code == 400
