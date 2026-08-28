"""Связка ядра прогрева с базой.

Ядро (`schedule`, `validators`, `matching`) не знает ни про SQLAlchemy, ни
про организации — здесь оно встречается с данными. Всё, что можно было
посчитать без базы, посчитано там; здесь только чтение, запись и перевод
одного в другое.
"""
from __future__ import annotations

import uuid
from datetime import date as Date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content_plan import PlannedPost
from app.models.knowledge import KnowledgeItem
from app.models.launch import Launch
from app.services.launch import matching, schedule
from app.services.launch.methodology import MEANING_BY_KEY, STAGE_BY_NUM
from app.services.launch.validators import (
    ORIGIN_HUMAN,
    ORIGIN_RULE,
    Report,
    SlotView,
    build_report,
)

#: Статусы единиц, которые нельзя молча двигать или удалять при пересчёте.
PROTECTED_STATUSES = ("published", "scheduled")


def _today() -> Date:
    return datetime.now(timezone.utc).date()


async def load_slots(db: AsyncSession, launch_id: uuid.UUID) -> list[PlannedPost]:
    rows = await db.scalars(
        select(PlannedPost)
        .where(PlannedPost.launch_id == launch_id)
        .order_by(PlannedPost.scheduled_date, PlannedPost.platform)
    )
    return list(rows.all())


async def load_bank(db: AsyncSession, organization_id: uuid.UUID) -> list[KnowledgeItem]:
    rows = await db.scalars(
        select(KnowledgeItem).where(
            KnowledgeItem.organization_id == organization_id,
            KnowledgeItem.type == "tezis",
        )
    )
    return list(rows.all())


def _idea_view(item: KnowledgeItem) -> matching.IdeaView:
    return matching.IdeaView(
        id=str(item.id),
        title=item.title,
        body=item.body or "",
        meaning=item.launch_meaning,
        fmt=item.content_format or "any",
        score=item.viral_score,
        used_at=item.last_used_at.isoformat() if item.last_used_at else None,
        markup_origin=item.markup_origin or ORIGIN_RULE,
    )


def _slot_view(post: PlannedPost) -> SlotView:
    return SlotView(
        day=post.scheduled_date,
        stage=post.launch_stage or 0,
        meaning=post.meaning or "",
        channel=post.platform,
        idea_id=str(post.knowledge_item_id) if post.knowledge_item_id else None,
        checkpoints=tuple(post.checkpoints or ()),
        triggers=(post.trigger_key,) if post.trigger_key else (),
        markup_origin=post.markup_origin or ORIGIN_RULE,
        has_proof=bool(post.has_proof),
        is_published=post.status == "published",
    )


async def generate_plan(
    db: AsyncSession,
    launch: Launch,
    *,
    today: Date | None = None,
    replace: bool = True,
) -> tuple[schedule.SchedulePlan, list[PlannedPost]]:
    """Развернуть календарь запуска в слоты контент-плана.

    Опубликованные единицы и всё, что человек закрепил руками, переживают
    пересчёт: наивная перегенерация затирала бы неделю работы, а откатить
    её было бы нечем.
    """
    today = today or _today()
    plan, slots = schedule.build_schedule(
        sales_open=launch.sales_open,
        sales_close=launch.sales_close,
        key_event=launch.key_event_date,
        today=today,
        intensity=launch.intensity,
        durations={int(k): v for k, v in (launch.durations or {}).items()},
    )

    existing = await load_slots(db, launch.id)
    protected: dict[tuple[Date, str], PlannedPost] = {}
    for post in existing:
        if post.status in PROTECTED_STATUSES or post.is_pinned:
            protected[(post.scheduled_date, post.platform)] = post
        elif replace:
            await db.delete(post)

    created: list[PlannedPost] = []
    for slot in slots:
        key = (slot.day, slot.channel)
        if key in protected:
            # День уже занят живой единицей — не трогаем её, только
            # обновляем принадлежность этапу.
            kept = protected[key]
            kept.launch_stage = slot.stage
            continue
        post = PlannedPost(
            organization_id=launch.organization_id,
            project_id=launch.project_id,
            launch_id=launch.id,
            platform=slot.channel,
            status="draft",
            scheduled_date=slot.day,
            launch_stage=slot.stage,
            meaning=slot.meaning,
            trigger_key=(
                MEANING_BY_KEY[slot.meaning].trigger_key
                if slot.meaning in MEANING_BY_KEY else None
            ),
            markup_origin=ORIGIN_RULE,
            is_last_day=slot.is_last_day,
            checkpoints=[],
        )
        db.add(post)
        created.append(post)

    await db.flush()
    return plan, created


async def assign_ideas(db: AsyncSession, launch: Launch) -> dict[str, int]:
    """Подставить идеи из банка в пустые слоты.

    Возвращает сводку: сколько заполнено и сколько осталось без идеи с
    названной причиной — молчаливый пустой слот хуже честного дефицита.
    """
    slots = [s for s in await load_slots(db, launch.id) if s.knowledge_item_id is None]
    slots = [s for s in slots if s.status not in PROTECTED_STATUSES]
    if not slots:
        return {"filled": 0, "empty": 0}

    bank = await load_bank(db, launch.organization_id)
    ideas = [_idea_view(i) for i in bank]
    by_id = {str(i.id): i for i in bank}

    # Идеи, уже стоящие в этом запуске, второй раз не берём.
    taken = {
        str(p.knowledge_item_id)
        for p in await load_slots(db, launch.id)
        if p.knowledge_item_id
    }

    specs = [(s.meaning or "", s.platform) for s in slots]
    assignments = matching.assign_ideas(specs, ideas, exclude_ids=frozenset(taken))

    filled = 0
    for assignment in assignments:
        slot = slots[assignment.slot_index]
        if not assignment.idea_id:
            slot.notes = assignment.reason
            continue
        item = by_id.get(assignment.idea_id)
        if item is None:
            continue
        slot.knowledge_item_id = item.id
        slot.talking_point_text = item.title
        slot.checkpoints = list(item.launch_checkpoints or [])
        slot.markup_origin = item.markup_origin or ORIGIN_RULE
        slot.notes = None
        filled += 1

    await db.flush()
    return {"filled": filled, "empty": len(slots) - filled}


async def markup_bank(
    db: AsyncSession, organization_id: uuid.UUID, *, overwrite: bool = False
) -> dict[str, int]:
    """Проставить черновую разметку идеям банка.

    Это именно черновик: источник помечается как `rule`, и проверки такую
    разметку в покрытие не засчитывают. Иначе продукт скажет «всё готово»
    на данных, которых никто не видел.
    """
    bank = await load_bank(db, organization_id)
    touched = 0
    for item in bank:
        if item.launch_meaning and not overwrite:
            continue
        if item.markup_origin == ORIGIN_HUMAN and not overwrite:
            continue
        draft = matching.draft_markup(_idea_view(item))
        item.launch_meaning = draft.meaning
        item.launch_checkpoints = list(draft.checkpoints)
        item.launch_triggers = list(draft.triggers)
        item.markup_origin = ORIGIN_RULE
        touched += 1
    await db.flush()
    return {"marked": touched, "total": len(bank)}


async def build_launch_report(db: AsyncSession, launch: Launch) -> Report:
    """Прогнать проверки по текущему состоянию запуска."""
    posts = await load_slots(db, launch.id)
    bank = await load_bank(db, launch.organization_id)
    histogram = matching.bank_histogram([_idea_view(i) for i in bank])
    readiness = {k: bool(v) for k, v in (launch.readiness or {}).items()} or None
    return build_report(
        [_slot_view(p) for p in posts],
        bank_by_meaning=histogram,
        readiness=readiness,
        bank_total=len(bank),
    )


def stage_summary(plan: schedule.SchedulePlan) -> list[dict]:
    """Окна этапов в виде, пригодном для интерфейса."""
    return [
        {
            "stage": w.stage,
            "key": w.key,
            "title": w.title,
            "purpose": STAGE_BY_NUM[w.stage].purpose,
            "start": w.start,
            "end": w.end,
            "days": w.days,
        }
        for w in plan.windows
    ]
