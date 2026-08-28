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


class SlotMarkupUpdate(BaseModel):
    """Подтверждение разметки человеком.

    Отдельная ручка, а не общий PATCH: подтверждение — это смысловое
    действие, после которого проверка начинает засчитывать покрытие.
    """

    checkpoints: list[str] | None = None
    trigger_key: str | None = None
    has_proof: bool | None = None
    is_peak: bool | None = None
    is_pinned: bool | None = None
    confirm: bool = True
    #: Версия слота, которую правил клиент. Защита от молчаливой потери
    #: чужих правок, когда план ведут вдвоём.
    version: int | None = None
