"""API модуля прогревов.

Прогрев живёт отдельным пространством: свой список, свой календарь, свои
проверки. В обычный контент-план его единицы по умолчанию не попадают —
иначе через месяц не понять, где ведёшь блог, а где готовишь запуск.
"""
import uuid
from datetime import date as Date, datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.content_plan import PlannedPost
from app.models.launch import Launch, LaunchEvidence, LaunchStoryLine
from app.schemas.launch import (
    AssignIdeasResponse,
    CheckpointRef,
    FindingOut,
    GeneratePlanRequest,
    LaunchCreate,
    LaunchOut,
    LaunchSlotOut,
    LaunchUpdate,
    MarkupBankResponse,
    MeaningRef,
    PlanResponse,
    QuestionRef,
    ReferenceResponse,
    EvidenceOut,
    EvidenceUpdate,
    ReportOut,
    SlotCreate,
    SlotUpdate,
    TaskOut,
    StageRef,
    StoryLineCreate,
    StoryLineOut,
    StoryLineUpdate,
    TriggerRef,
)
from app.services.launch import evidence as ev_service, service
from app.services.launch.methodology import (
    CHECKPOINTS,
    MEANINGS,
    QUESTIONS,
    STAGES,
    TRIGGERS,
)
from app.services.launch.schedule import (
    CHANNEL_REELS,
    CHANNEL_STORIES,
    CHANNEL_TELEGRAM,
    LaunchDatesError,
    plan_windows,
    validate_dates,
)
from app.services.launch.validators import ORIGIN_HUMAN

router = APIRouter(prefix="/launches", tags=["launches"])


def _today() -> Date:
    return datetime.now(timezone.utc).date()


def _out(launch: Launch) -> LaunchOut:
    """Запуск наружу вместе с посчитанным режимом."""
    dto = LaunchOut.model_validate(launch)
    dto.mode = ev_service.launch_mode(launch, _today())
    return dto


async def _owned(db, launch_id: uuid.UUID, org_id: uuid.UUID) -> Launch:
    obj = await db.scalar(
        select(Launch).where(Launch.id == launch_id, Launch.organization_id == org_id)
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Запуск не найден")
    return obj


# ---------------------------------------------------------------------------
# Справочники
# ---------------------------------------------------------------------------


@router.get("/reference", response_model=ReferenceResponse)
async def reference(current: CurrentUser) -> ReferenceResponse:
    """Справочники методологии: этапы, рубрики, рычаги, смыслы."""
    return ReferenceResponse(
        stages=[
            StageRef(
                num=s.num, key=s.key, title=s.title, purpose=s.purpose,
                min_days=s.min_days, default_days=s.default_days,
                max_days=s.max_days, background=s.background,
            )
            for s in STAGES
        ],
        meanings=[
            MeaningRef(
                key=m.key, num=m.num, title=m.title, question=m.question,
                trigger_key=m.trigger_key, ours=m.ours,
            )
            for m in MEANINGS
        ],
        triggers=[
            TriggerRef(
                key=t.key, title=t.title, category=t.category, why=t.why,
                stage=t.stage, cross_stage=t.cross_stage,
            )
            for t in TRIGGERS
        ],
        checkpoints=[
            CheckpointRef(key=c.key, question=c.question, title=c.title)
            for c in CHECKPOINTS
        ],
        questions=[
            QuestionRef(num=q.num, title=q.title, why=q.why) for q in QUESTIONS
        ],
    )


# ---------------------------------------------------------------------------
# Запуски
# ---------------------------------------------------------------------------


@router.get("", response_model=list[LaunchOut])
async def list_launches(
    current: CurrentUser,
    db: DbSession,
    include_archived: bool = Query(default=False),
) -> list[LaunchOut]:
    stmt = select(Launch).where(Launch.organization_id == current.organization_id)
    if not include_archived:
        stmt = stmt.where(Launch.status != "archived")
    rows = await db.scalars(stmt.order_by(Launch.sales_open.desc()))
    return [_out(r) for r in rows.all()]


@router.post("", response_model=LaunchOut, status_code=status.HTTP_201_CREATED)
async def create_launch(
    payload: LaunchCreate, current: CurrentUser, db: DbSession
) -> LaunchOut:
    try:
        validate_dates(
            sales_open=payload.sales_open,
            sales_close=payload.sales_close,
            key_event=payload.key_event_date,
            today=datetime.now(timezone.utc).date(),
        )
    except LaunchDatesError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    launch = Launch(
        organization_id=current.organization_id,
        project_id=payload.project_id,
        name=payload.name,
        product_name=payload.product_name,
        sales_open=payload.sales_open,
        sales_close=payload.sales_close,
        key_event_date=payload.key_event_date,
        key_event_type=payload.key_event_type,
        launch_number=payload.launch_number,
        intensity=payload.intensity,
        timezone=payload.timezone,
        waitlist_goal=payload.waitlist_goal,
        notes=payload.notes,
        readiness={},
        durations={},
    )
    db.add(launch)
    await db.flush()
    return _out(launch)


@router.get("/{launch_id}", response_model=LaunchOut)
async def get_launch(
    launch_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> LaunchOut:
    return _out(await _owned(db, launch_id, current.organization_id))


@router.patch("/{launch_id}", response_model=LaunchOut)
async def update_launch(
    launch_id: uuid.UUID,
    payload: LaunchUpdate,
    current: CurrentUser,
    db: DbSession,
) -> LaunchOut:
    launch = await _owned(db, launch_id, current.organization_id)
    data = payload.model_dump(exclude_unset=True)

    # Даты проверяем на итоговом наборе, а не на присланном куске: иначе
    # можно передвинуть закрытие раньше открытия двумя запросами.
    try:
        validate_dates(
            sales_open=data.get("sales_open", launch.sales_open),
            sales_close=data.get("sales_close", launch.sales_close),
            key_event=data.get("key_event_date", launch.key_event_date),
        )
    except LaunchDatesError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    for field, value in data.items():
        setattr(launch, field, value)
    await db.flush()
    return _out(launch)


@router.post("/{launch_id}/archive", response_model=LaunchOut)
async def archive_launch(
    launch_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> LaunchOut:
    """Архивировать запуск.

    Именно архивировать, а не удалять: вместе с запуском иначе исчезают
    опубликованные единицы и вся фактура для разбора следующего потока.
    """
    launch = await _owned(db, launch_id, current.organization_id)
    launch.status = "archived"
    launch.archived_at = datetime.now(timezone.utc)
    await db.flush()
    return _out(launch)


# ---------------------------------------------------------------------------
# План
# ---------------------------------------------------------------------------


@router.get("/{launch_id}/plan", response_model=PlanResponse)
async def get_plan(
    launch_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> PlanResponse:
    launch = await _owned(db, launch_id, current.organization_id)
    try:
        plan = plan_windows(
            sales_open=launch.sales_open,
            sales_close=launch.sales_close,
            key_event=launch.key_event_date,
            durations={int(k): v for k, v in (launch.durations or {}).items()},
        )
    except LaunchDatesError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    slots = await service.load_slots(db, launch.id)
    return PlanResponse(
        windows=service.stage_summary(plan),
        slots=[LaunchSlotOut.model_validate(s) for s in slots],
        compressed=plan.compressed,
        dropped=plan.dropped,
    )


@router.post("/{launch_id}/plan", response_model=PlanResponse)
async def generate_plan(
    launch_id: uuid.UUID,
    payload: GeneratePlanRequest,
    current: CurrentUser,
    db: DbSession,
) -> PlanResponse:
    """Развернуть календарь запуска.

    Опубликованные и закреплённые единицы переживают пересчёт: без этого
    перенос даты стоил бы человеку недели работы без возможности отката.
    """
    launch = await _owned(db, launch_id, current.organization_id)
    try:
        plan, _ = await service.generate_plan(db, launch, replace=payload.replace)
    except LaunchDatesError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    if payload.assign_ideas:
        await service.assign_ideas(db, launch)

    slots = await service.load_slots(db, launch.id)
    return PlanResponse(
        windows=service.stage_summary(plan),
        slots=[LaunchSlotOut.model_validate(s) for s in slots],
        compressed=plan.compressed,
        dropped=plan.dropped,
    )


@router.post("/{launch_id}/assign-ideas", response_model=AssignIdeasResponse)
async def assign_ideas(
    launch_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> AssignIdeasResponse:
    launch = await _owned(db, launch_id, current.organization_id)
    return AssignIdeasResponse(**await service.assign_ideas(db, launch))


@router.post("/markup-bank", response_model=MarkupBankResponse)
async def markup_bank(
    current: CurrentUser,
    db: DbSession,
    overwrite: bool = Query(default=False),
) -> MarkupBankResponse:
    """Проставить черновую разметку идеям банка.

    Черновую: источник помечается как `rule`, и проверки такую разметку в
    покрытие не засчитывают, пока человек её не подтвердит.
    """
    return MarkupBankResponse(
        **await service.markup_bank(db, current.organization_id, overwrite=overwrite)
    )


# ---------------------------------------------------------------------------
# Проверки
# ---------------------------------------------------------------------------


@router.get("/{launch_id}/report", response_model=ReportOut)
async def get_report(
    launch_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> ReportOut:
    """Что в прогреве не так и чем это грозит."""
    launch = await _owned(db, launch_id, current.organization_id)
    report = await service.build_launch_report(db, launch)
    return ReportOut(
        ready=report.ready,
        findings=[
            FindingOut(
                code=f.code, severity=f.severity, title=f.title, message=f.message,
                fix_days=list(f.fix_days), affected=f.affected, verified=f.verified,
            )
            for f in report.sorted_findings()
        ],
        checkpoints_confirmed=report.checkpoints_confirmed,
        checkpoints_claimed=report.checkpoints_claimed,
        checkpoints_total=report.checkpoints_total,
        triggers_used=report.triggers_used,
        triggers_total=report.triggers_total,
        slots_total=report.slots_total,
        slots_with_idea=report.slots_with_idea,
        bank_total=report.bank_total,
        bank_marked=report.bank_marked,
    )


# ---------------------------------------------------------------------------
# Слоты
# ---------------------------------------------------------------------------


@router.post(
    "/{launch_id}/slots",
    response_model=LaunchSlotOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_slot(
    launch_id: uuid.UUID,
    payload: SlotCreate,
    current: CurrentUser,
    db: DbSession,
) -> LaunchSlotOut:
    """Добавить слот руками.

    Такой слот сразу закрепляется: человек поставил его осознанно, и
    пересборка плана не должна его смывать. Дата обязана попадать в окно
    запуска — слот вне оси не показать ни на одном экране.
    """
    launch = await _owned(db, launch_id, current.organization_id)
    if payload.platform not in (CHANNEL_STORIES, CHANNEL_REELS, CHANNEL_TELEGRAM):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неизвестный канал")

    window = await service.window_for(db, launch, payload.scheduled_date)
    if window is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Эта дата вне оси запуска. Сначала расширьте окно или пересоберите план.",
        )

    slot = PlannedPost(
        organization_id=launch.organization_id,
        project_id=launch.project_id,
        launch_id=launch.id,
        scheduled_date=payload.scheduled_date,
        platform=payload.platform,
        status="draft",
        launch_stage=window.stage,
        rubric=payload.rubric,
        meaning=payload.meaning,
        knowledge_item_id=payload.knowledge_item_id,
        markup_origin=ORIGIN_HUMAN,
        is_pinned=True,
        is_last_day=window.key == "sales" and payload.scheduled_date == window.end,
        empty_reason=None if payload.knowledge_item_id else "слот добавлен вручную — идею надо выбрать",
    )
    db.add(slot)
    await db.flush()
    return LaunchSlotOut.model_validate(slot)


@router.patch("/{launch_id}/slots/{slot_id}", response_model=LaunchSlotOut)
async def update_slot(
    launch_id: uuid.UUID,
    slot_id: uuid.UUID,
    payload: SlotUpdate,
    current: CurrentUser,
    db: DbSession,
) -> LaunchSlotOut:
    """Поправить слот: разметка, перенос, отметки по факту публикации.

    Версия сверяется всегда, когда её прислали: слот правят из нескольких
    вкладок, и правка второго редактора не должна исчезать молча.

    Перенос меняет этап вместе с датой — иначе слот повиснет между этапами
    и попадёт не в ту квоту при следующей проверке.
    """
    launch = await _owned(db, launch_id, current.organization_id)
    slot = await db.scalar(
        select(PlannedPost).where(
            PlannedPost.id == slot_id,
            PlannedPost.launch_id == launch.id,
        )
    )
    if slot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Слот не найден")

    if payload.version is not None and payload.version != slot.version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Слот уже изменили в другом окне. Обновите план и повторите — "
            "иначе чужая правка потеряется.",
        )

    data = payload.model_dump(exclude_unset=True)

    if "scheduled_date" in data and data["scheduled_date"] is not None:
        window = await service.window_for(db, launch, data["scheduled_date"])
        if window is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Эта дата вне оси запуска"
            )
        slot.scheduled_date = data["scheduled_date"]
        slot.launch_stage = window.stage
        slot.is_last_day = window.key == "sales" and slot.scheduled_date == window.end

    for field in (
        "rubric", "meaning", "checkpoints", "trigger_key", "knowledge_item_id",
        "talking_point_text", "has_proof", "is_peak", "is_pinned",
        "status", "reaction", "draft_state", "chars", "full_text",
    ):
        if field in data and data[field] is not None:
            setattr(slot, field, data[field])

    # Идея поставлена — причина «пусто» больше не про этот слот.
    if data.get("knowledge_item_id") or data.get("talking_point_text"):
        slot.empty_reason = None

    if payload.confirm:
        slot.markup_origin = ORIGIN_HUMAN

    slot.version = (slot.version or 1) + 1
    await db.flush()
    return LaunchSlotOut.model_validate(slot)


@router.delete(
    "/{launch_id}/slots/{slot_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_slot(
    launch_id: uuid.UUID,
    slot_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
) -> None:
    """Убрать слот.

    Опубликованное не удаляется: это уже история запуска, и разбор считает
    по ней. Такой слот можно только отметить пропущенным.
    """
    launch = await _owned(db, launch_id, current.organization_id)
    slot = await db.scalar(
        select(PlannedPost).where(
            PlannedPost.id == slot_id,
            PlannedPost.launch_id == launch.id,
        )
    )
    if slot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Слот не найден")
    if slot.status == "published":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Опубликованный слот не удаляется: на нём стоит разбор. "
            "Отметьте его пропущенным, если пост не выходил.",
        )
    await db.delete(slot)
    await db.flush()


# ---------------------------------------------------------------------------
# Фактура
# ---------------------------------------------------------------------------


@router.get("/{launch_id}/evidence", response_model=list[EvidenceOut])
async def get_evidence(
    launch_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> list[EvidenceOut]:
    """Состояние всех сорока смыслов.

    Возвращаются все, а не только сохранённые: отсутствие строки — это
    «нечем доказать», и клиент не должен догадываться об этом сам.
    """
    launch = await _owned(db, launch_id, current.organization_id)
    saved = await ev_service.load_evidence(db, launch.id)
    out: list[EvidenceOut] = []
    for c in CHECKPOINTS:
        row = saved.get(c.key)
        out.append(
            EvidenceOut.model_validate(row)
            if row is not None
            else EvidenceOut(meaning_key=c.key, state=ev_service.STATE_NONE)
        )
    return out


@router.patch("/{launch_id}/evidence/{meaning_key}", response_model=EvidenceOut)
async def update_evidence(
    launch_id: uuid.UUID,
    meaning_key: str,
    payload: EvidenceUpdate,
    current: CurrentUser,
    db: DbSession,
) -> EvidenceOut:
    """Проставить состояние смысла."""
    launch = await _owned(db, launch_id, current.organization_id)
    try:
        row = await ev_service.set_evidence(
            db,
            launch.id,
            meaning_key,
            state=payload.state,
            proof_note=payload.proof_note,
            proof_url=payload.proof_url,
            task_dismissed=payload.task_dismissed,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return EvidenceOut.model_validate(row)


@router.get("/{launch_id}/tasks", response_model=list[TaskOut])
async def get_tasks(
    launch_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> list[TaskOut]:
    """Задачи на добычу фактуры.

    Не хранятся: это незакрытые смыслы плюс дедлайн от первого дня плана,
    где смысл понадобится. Считаются на каждый запрос, поэтому не могут
    разойтись с фактурой.
    """
    launch = await _owned(db, launch_id, current.organization_id)
    rows = await ev_service.tasks_for(db, launch)
    return [TaskOut(**r) for r in rows]


# ---------------------------------------------------------------------------
# Сюжетные линии
# ---------------------------------------------------------------------------


@router.get("/{launch_id}/story-lines", response_model=list[StoryLineOut])
async def list_story_lines(
    launch_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> list[StoryLineOut]:
    launch = await _owned(db, launch_id, current.organization_id)
    rows = await db.scalars(
        select(LaunchStoryLine)
        .where(LaunchStoryLine.launch_id == launch.id)
        .order_by(LaunchStoryLine.announced_on)
    )
    return [StoryLineOut.model_validate(r) for r in rows.all()]


@router.post(
    "/{launch_id}/story-lines",
    response_model=StoryLineOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_story_line(
    launch_id: uuid.UUID,
    payload: StoryLineCreate,
    current: CurrentUser,
    db: DbSession,
) -> StoryLineOut:
    launch = await _owned(db, launch_id, current.organization_id)
    line = LaunchStoryLine(launch_id=launch.id, **payload.model_dump())
    db.add(line)
    await db.flush()
    return StoryLineOut.model_validate(line)


@router.patch("/{launch_id}/story-lines/{line_id}", response_model=StoryLineOut)
async def update_story_line(
    launch_id: uuid.UUID,
    line_id: uuid.UUID,
    payload: StoryLineUpdate,
    current: CurrentUser,
    db: DbSession,
) -> StoryLineOut:
    """Поправить линию, в том числе привязать анонс и раскрытие к постам.

    `is_closed` здесь не поле ввода, а следствие: линия закрыта тогда и
    только тогда, когда под раскрытие стоит конкретный слот и он не позже
    закрытия продаж. Дать это поле снаружи значило бы разрешить пометить
    закрытым обещание, которого никто не увидит.
    """
    launch = await _owned(db, launch_id, current.organization_id)
    line = await db.scalar(
        select(LaunchStoryLine).where(
            LaunchStoryLine.id == line_id,
            LaunchStoryLine.launch_id == launch.id,
        )
    )
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Линия не найдена")

    data = payload.model_dump(exclude_unset=True)

    for field in ("announce_slot_id", "close_slot_id"):
        if field not in data:
            continue
        slot_id = data[field]
        if slot_id is not None:
            slot = await db.scalar(
                select(PlannedPost).where(
                    PlannedPost.id == slot_id,
                    PlannedPost.launch_id == launch.id,
                )
            )
            if slot is None:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Слот не найден в этом запуске",
                )
            # Дата линии следует за постом, а не наоборот.
            if field == "announce_slot_id":
                line.announced_on = slot.scheduled_date
                slot.line_role = "announce"
            else:
                line.closes_on = slot.scheduled_date
                slot.line_role = "close"
            slot.story_line_id = line.id
        setattr(line, field, slot_id)

    for field in ("title", "payoff", "announced_on", "closes_on"):
        if field in data and data[field] is not None:
            setattr(line, field, data[field])

    line.is_closed = bool(line.close_slot_id) and (
        launch.sales_close is None
        or line.closes_on is None
        or line.closes_on <= launch.sales_close
    )
    await db.flush()
    return StoryLineOut.model_validate(line)


@router.delete(
    "/{launch_id}/story-lines/{line_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_story_line(
    launch_id: uuid.UUID, line_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> None:
    launch = await _owned(db, launch_id, current.organization_id)
    line = await db.scalar(
        select(LaunchStoryLine).where(
            LaunchStoryLine.id == line_id,
            LaunchStoryLine.launch_id == launch.id,
        )
    )
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сюжетная линия не найдена")
    await db.delete(line)
