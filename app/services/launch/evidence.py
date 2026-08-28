"""Фактура запуска и задачи на добычу.

Фактура — ответ на вопрос «чем вообще есть что доказывать». Сорок смыслов
покупателя, у каждого одно из трёх состояний: есть с пруфом · заявлено
словами · нечем доказать. В покрытие идёт только первое.

Задачи здесь не хранятся и не создаются. Задача — это состояние `none` плюс
дедлайн, посчитанный от первого дня плана, где смысл понадобится. Заводить
под это строку в базе значит держать вторую копию того же факта: смысл
закрыли, а задача осталась висеть, потому что её забыли пометить. Поэтому
персистится ровно одно поле, которое не выводится, — `task_dismissed`.
"""
from __future__ import annotations

import uuid
from datetime import date as Date, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content_plan import PlannedPost
from app.models.launch import Launch, LaunchEvidence
from app.services.launch.methodology import CHECKPOINTS, CHECKPOINT_BY_KEY

#: За сколько дней до слота фактура должна быть на руках.
#: Два дня — минимум, за который можно успеть снять и смонтировать.
LEAD_DAYS = 2

STATE_PROOF = "proof"
STATE_CLAIMED = "claimed"
STATE_NONE = "none"


async def load_evidence(
    db: AsyncSession, launch_id: uuid.UUID
) -> dict[str, LaunchEvidence]:
    """Сохранённая фактура запуска, ключ — смысл."""
    rows = await db.scalars(
        select(LaunchEvidence).where(LaunchEvidence.launch_id == launch_id)
    )
    return {r.meaning_key: r for r in rows.all()}


async def evidence_map(db: AsyncSession, launch_id: uuid.UUID) -> dict[str, str]:
    """Состояние каждого из сорока смыслов.

    Отсутствие строки — это `none`, а не пробел: запуск, в котором фактуру
    ещё не трогали, ничем не доказан, и проверка должна видеть именно это.
    """
    saved = await load_evidence(db, launch_id)
    return {c.key: (saved[c.key].state if c.key in saved else STATE_NONE) for c in CHECKPOINTS}


async def set_evidence(
    db: AsyncSession,
    launch_id: uuid.UUID,
    meaning_key: str,
    *,
    state: str,
    proof_note: str | None = None,
    proof_url: str | None = None,
    task_dismissed: bool | None = None,
) -> LaunchEvidence:
    """Проставить состояние смысла.

    UPSERT, а не «прочитать и решить»: фактуру щёлкают быстро и из разных
    мест, и гонка между чтением и записью здесь реальна.
    """
    if meaning_key not in CHECKPOINT_BY_KEY:
        raise ValueError(f"Неизвестный смысл: {meaning_key}")

    values: dict[str, object] = {
        "launch_id": launch_id,
        "meaning_key": meaning_key,
        "state": state,
        "proof_note": proof_note,
        "proof_url": proof_url,
    }
    if task_dismissed is not None:
        values["task_dismissed"] = task_dismissed

    stmt = pg_insert(LaunchEvidence).values(**values)
    update = {k: v for k, v in values.items() if k not in ("launch_id", "meaning_key")}
    stmt = stmt.on_conflict_do_update(
        index_elements=[LaunchEvidence.launch_id, LaunchEvidence.meaning_key],
        set_=update,
    ).returning(LaunchEvidence)
    row = await db.scalar(stmt)
    await db.flush()
    return row


async def tasks_for(
    db: AsyncSession, launch: Launch, *, today: Date | None = None
) -> list[dict]:
    """Задачи на добычу: незакрытые смыслы с дедлайном от дня, где они нужны.

    Смыслы, которых нет в плане, тоже попадают в список — просто без даты:
    их спрашивают в порядке вопросов покупателя, когда ближайшие закрыты.
    """
    saved = await load_evidence(db, launch.id)

    rows = await db.execute(
        select(PlannedPost.meaning, PlannedPost.scheduled_date)
        .where(
            PlannedPost.launch_id == launch.id,
            PlannedPost.meaning.is_not(None),
            PlannedPost.scheduled_date.is_not(None),
        )
        .order_by(PlannedPost.scheduled_date)
    )
    by_meaning: dict[str, list[Date]] = {}
    for meaning, day in rows.all():
        by_meaning.setdefault(meaning, []).append(day)

    out: list[dict] = []
    for c in CHECKPOINTS:
        row = saved.get(c.key)
        if row is not None and (row.state == STATE_PROOF or row.task_dismissed):
            continue
        if row is None or row.state == STATE_NONE:
            days = by_meaning.get(c.key, [])
            needed = days[0] - timedelta(days=LEAD_DAYS) if days else None
            out.append({
                "meaning_key": c.key,
                "title": c.title,
                "question": c.question,
                "needed_by": needed,
                "slot_dates": days[:4],
            })

    # Сначала то, что нужно раньше; смыслы без слотов — в хвост по порядку
    # вопросов покупателя.
    out.sort(key=lambda t: (t["needed_by"] is None, t["needed_by"] or Date.max, t["question"]))
    return out


def launch_mode(launch: Launch, today: Date) -> str:
    """Режим запуска: черновик · прогрев · окно продаж · закрыт.

    Считается, а не хранится. Всё, кроме черновика, однозначно выводится из
    дат, и хранимая копия начинает врать при первом же переносе окна.
    """
    if launch.archived_at is not None:
        return "closed"
    if launch.sales_close is not None and today > launch.sales_close:
        return "closed"
    if today >= launch.sales_open:
        return "sales"
    if launch.unrolled_on is None:
        return "draft"
    return "warm"
