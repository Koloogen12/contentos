import uuid
from datetime import date, datetime, time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PlatformT = Literal[
    "telegram", "instagram", "linkedin", "twitter", "article", "carousel", "reels", "hooks"
]
PostStatusT = Literal["draft", "ready", "scheduled", "published", "skipped"]
PillarT = Literal["R1", "R2", "R3", "R4"]


class PlannedPostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    canvas_id: uuid.UUID | None
    node_id: uuid.UUID | None
    project_id: uuid.UUID | None

    platform: PlatformT
    hook: str
    body: str
    cta: str
    full_text: str
    talking_point_text: str | None

    scheduled_date: date | None
    scheduled_time: time | None

    status: PostStatusT
    pillar: PillarT | None
    tags: list[str]
    notes: str | None

    published_at: datetime | None
    metrics: dict[str, Any]

    created_at: datetime
    updated_at: datetime


class PlannedPostCreate(BaseModel):
    platform: PlatformT
    hook: str = ""
    body: str = ""
    cta: str = ""
    full_text: str = ""
    talking_point_text: str | None = None

    canvas_id: uuid.UUID | None = None
    node_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None

    scheduled_date: date | None = None
    scheduled_time: time | None = None

    status: PostStatusT = "draft"
    pillar: PillarT | None = None
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)


class PlannedPostUpdate(BaseModel):
    platform: PlatformT | None = None
    hook: str | None = None
    body: str | None = None
    cta: str | None = None
    full_text: str | None = None
    talking_point_text: str | None = None
    project_id: uuid.UUID | None = None

    scheduled_date: date | None = None
    scheduled_time: time | None = None
    status: PostStatusT | None = None
    pillar: PillarT | None = None
    tags: list[str] | None = None
    notes: str | None = None
    metrics: dict[str, Any] | None = None


class ScheduleFromNodeRequest(BaseModel):
    """Request body for POST /nodes/{id}/schedule (create PlannedPost from format node)."""

    scheduled_date: date | None = None
    scheduled_time: time | None = None
    pillar: PillarT | None = None
    tags: list[str] = Field(default_factory=list)


# ----- Week summary -----


class WeekDayOut(BaseModel):
    date: date
    day_name: str  # "Понедельник"
    posts: list[PlannedPostOut]
    is_empty: bool


class WeekStatsOut(BaseModel):
    total_scheduled: int
    total_ready: int
    empty_days: int
    platforms: dict[str, int]
    pillars: dict[str, int]


class WeekResponse(BaseModel):
    week_start: date
    week_end: date
    days: list[WeekDayOut]
    stats: WeekStatsOut


# ----- Aggregated stats -----


class TopPostOut(BaseModel):
    """Compact view of a published post for the leaderboard."""

    id: uuid.UUID
    platform: PlatformT
    hook: str
    full_text: str
    pillar: PillarT | None
    published_at: datetime | None
    metrics: dict[str, Any]


class StatsResponse(BaseModel):
    publishing_streak: int
    publishing_streak_record: int
    total_published: int
    this_week_published: int
    this_month_published: int
    content_mix: dict[str, int]  # percent per pillar
    platform_mix: dict[str, int]  # percent per platform
    top_posts: list[TopPostOut]


# ----- "What to write today" -----


class WhatToWriteRecommendation(BaseModel):
    type: Literal["dormant_gem", "pillar_balance", "top_score"]
    title: str
    knowledge_item_id: uuid.UUID | None
    knowledge_item_title: str | None
    knowledge_item_body: str | None
    pillar: PillarT | None
    viral_score: int | None


class WhatToWriteResponse(BaseModel):
    date: date
    priority_pillar: PillarT
    pillar_reason: str
    recommendations: list[WhatToWriteRecommendation]
