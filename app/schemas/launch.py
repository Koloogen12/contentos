"""Схемы модуля прогревов."""
from __future__ import annotations

import uuid
from datetime import date as Date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

LaunchStatusT = Literal["draft", "active", "paused", "done", "archived"]
IntensityT = Literal["light", "normal", "heavy"]
MarkupOriginT = Literal["rule", "llm", "human"]


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------


class LaunchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    product_name: str | None = Field(default=None, max_length=255)
    #: Единственная обязательная дата. Всё остальное разворачивается от неё.
    sales_open: Date
    sales_close: Date | None = None
    key_event_date: Date | None = None
    key_event_type: str | None = Field(default=None, max_length=50)
    launch_number: int = Field(default=1, ge=1, le=99)
    intensity: IntensityT = "normal"
    timezone: str = Field(default="Europe/Moscow", max_length=64)
    project_id: uuid.UUID | None = None
    waitlist_goal: int | None = Field(default=None, ge=0)
    notes: str | None = None


class LaunchUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    product_name: str | None = Field(default=None, max_length=255)
    sales_open: Date | None = None
    sales_close: Date | None = None
    key_event_date: Date | None = None
    key_event_type: str | None = Field(default=None, max_length=50)
    launch_number: int | None = Field(default=None, ge=1, le=99)
    intensity: IntensityT | None = None
    timezone: str | None = Field(default=None, max_length=64)
    status: LaunchStatusT | None = None
    readiness: dict[str, bool] | None = None
    durations: dict[str, int] | None = None
    waitlist_goal: int | None = Field(default=None, ge=0)
    notes: str | None = None


class LaunchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    product_name: str | None
    sales_open: Date
    sales_close: Date | None
    key_event_date: Date | None
    key_event_type: str | None
    launch_number: int
    intensity: str
    timezone: str
    status: str
    readiness: dict[str, Any]
    durations: dict[str, Any]
    waitlist_goal: int | None
    notes: str | None
    archived_at: datetime | None
    created_at: datetime

    #: Рамка запуска.
    price: str | None = None
    audience: str | None = None
    collect: str | None = None
    waitlist: int | None = None
    paid: int | None = None
    paid_goal: int | None = None
    unrolled_on: Date | None = None

    #: Режим запуска: черновик · прогрев идёт · окно продаж · закрыт.
    #:
    #: Считается, а не хранится: всё, кроме черновика, однозначно выводится
    #: из дат. Клиенту отдаётся готовым, чтобы четыре места в интерфейсе не
    #: вывели его четырьмя способами.
    mode: str = "draft"


# ---------------------------------------------------------------------------
# План
# ---------------------------------------------------------------------------


class StageWindowOut(BaseModel):
    stage: int
    key: str
    title: str
    purpose: str
    start: Date
    end: Date
    days: int


class LaunchSlotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scheduled_date: Date | None
    platform: str
    status: str
    launch_stage: int | None
    #: Рубрика конструктора (одна из двенадцати).
    rubric: str | None
    #: Смысл покупателя (один из сорока).
    meaning: str | None
    trigger_key: str | None
    checkpoints: list[str]
    markup_origin: str | None
    has_proof: bool
    is_last_day: bool
    is_peak: bool
    is_pinned: bool
    knowledge_item_id: uuid.UUID | None
    talking_point_text: str | None
    hook: str
    full_text: str
    #: Почему слот пуст — показывается пользователю дословно.
    notes: str | None
    empty_reason: str | None = None
    draft_state: str | None = None
    chars: int | None = None
    reaction: int | None = None
    line_role: str | None = None
    story_line_id: uuid.UUID | None = None
    version: int


class PlanResponse(BaseModel):
    windows: list[StageWindowOut]
    slots: list[LaunchSlotOut]
    #: Что пришлось сжать или выбросить, чтобы уложиться в срок.
    compressed: list[str] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)


class GeneratePlanRequest(BaseModel):
    #: Пересобрать план, сохранив опубликованное и закреплённое.
    replace: bool = True
    #: Сразу подставить идеи из банка.
    assign_ideas: bool = True


class AssignIdeasResponse(BaseModel):
    filled: int
    empty: int


class MarkupBankResponse(BaseModel):
    marked: int
    total: int


# ---------------------------------------------------------------------------
# Проверки
# ---------------------------------------------------------------------------


class FindingOut(BaseModel):
    code: str
    severity: Literal["critical", "high", "medium"]
    title: str
    message: str
    fix_days: list[Date] = Field(default_factory=list)
    affected: int = 0
    verified: bool = True


class ReportOut(BaseModel):
    ready: bool
    findings: list[FindingOut]
    checkpoints_confirmed: int
    checkpoints_claimed: int
    checkpoints_total: int
    triggers_used: int
    triggers_total: int
    slots_total: int
    slots_with_idea: int
    bank_total: int
    bank_marked: int


# ---------------------------------------------------------------------------
# Сюжетные линии
# ---------------------------------------------------------------------------


class StoryLineCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    payoff: str | None = None
    announced_on: Date | None = None
    closes_on: Date | None = None


class StoryLineUpdate(BaseModel):
    #: Посты, которыми линия открывается и закрывается.
    #:
    #: Именно они, а не даты, определяют, закрыта линия или нет: дата в
    #: календаре ничего не обещает аудитории, пост обещает.
    announce_slot_id: uuid.UUID | None = None
    close_slot_id: uuid.UUID | None = None

    title: str | None = Field(default=None, min_length=1, max_length=255)
    payoff: str | None = None
    announced_on: Date | None = None
    closes_on: Date | None = None
    is_closed: bool | None = None


class StoryLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    launch_id: uuid.UUID
    title: str
    payoff: str | None
    announced_on: Date | None
    closes_on: Date | None
    is_closed: bool
    announce_slot_id: uuid.UUID | None = None
    close_slot_id: uuid.UUID | None = None


# ---------------------------------------------------------------------------
# Справочники
# ---------------------------------------------------------------------------


class StageRef(BaseModel):
    num: int
    key: str
    title: str
    purpose: str
    min_days: int
    default_days: int
    max_days: int
    background: bool


class MeaningRef(BaseModel):
    key: str
    num: int
    title: str
    question: int
    trigger_key: str
    ours: bool


class TriggerRef(BaseModel):
    key: str
    title: str
    category: str
    why: str
    stage: int | None
    cross_stage: bool


class CheckpointRef(BaseModel):
    key: str
    question: int
    title: str


class QuestionRef(BaseModel):
    num: int
    title: str
    why: str


class ReferenceResponse(BaseModel):
    stages: list[StageRef]
    meanings: list[MeaningRef]
    triggers: list[TriggerRef]
    checkpoints: list[CheckpointRef]
    questions: list[QuestionRef]


class SlotUpdate(BaseModel):
    """Правка слота: разметка, перенос, отметки по факту публикации.

    Одна ручка на всё, но `confirm` остаётся отдельным флагом: подтверждение
    разметки — смысловое действие, после которого проверка начинает
    засчитывать покрытие, и оно не должно случайно проезжать вместе с
    переносом даты.

    `version` обязателен для всех правок, кроме чтения: слот правят из
    нескольких вкладок, и молча перетирать чужое нельзя.
    """

    scheduled_date: Date | None = None
    rubric: str | None = None
    meaning: str | None = None
    checkpoints: list[str] | None = None
    trigger_key: str | None = None
    knowledge_item_id: uuid.UUID | None = None
    talking_point_text: str | None = None
    has_proof: bool | None = None
    is_peak: bool | None = None
    is_pinned: bool | None = None

    #: Ведение запуска.
    status: str | None = None
    reaction: int | None = Field(default=None, ge=1, le=3)
    draft_state: str | None = None
    chars: int | None = None
    full_text: str | None = None

    confirm: bool = False
    version: int | None = None


#: Старое имя ручки. Оставлено, чтобы не ломать вызовы, которые уже есть.
SlotMarkupUpdate = SlotUpdate


class SlotCreate(BaseModel):
    """Слот, добавленный руками.

    Такой слот сразу закрепляется: человек поставил его осознанно, и
    пересборка плана не должна его смывать.
    """

    scheduled_date: Date
    platform: str
    rubric: str | None = None
    meaning: str | None = None
    knowledge_item_id: uuid.UUID | None = None


class EvidenceOut(BaseModel):
    """Состояние одного смысла покупателя."""

    model_config = ConfigDict(from_attributes=True)

    meaning_key: str
    state: str
    proof_note: str | None = None
    proof_url: str | None = None
    task_dismissed: bool = False


class EvidenceUpdate(BaseModel):
    state: str = Field(pattern="^(proof|claimed|none)$")
    proof_note: str | None = None
    proof_url: str | None = None
    task_dismissed: bool | None = None


class TaskOut(BaseModel):
    """Задача на добычу фактуры.

    Не хранится: это состояние `none` плюс дедлайн, посчитанный от первого
    дня плана, где смысл понадобится. Вторая копия того же факта разошлась бы
    с фактурой на первой же правке.
    """

    meaning_key: str
    title: str
    question: int
    #: К какому дню нужно успеть — за два дня до первого слота с этим смыслом.
    needed_by: Date | None
    #: Дни плана, где смысл стоит.
    slot_dates: list[Date]
    #: Версия слота, которую правил клиент. Защита от молчаливой потери
    #: чужих правок, когда план ведут вдвоём.
    version: int | None = None
