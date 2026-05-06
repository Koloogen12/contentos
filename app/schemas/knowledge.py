import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

KnowledgeTypeT = Literal[
    "tezis", "reference", "audience", "voice_rule", "content_theme", "manifesto"
]
PillarT = Literal["R1", "R2", "R3", "R4"]


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    color: str
    context: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    color: str = "#6366f1"
    context: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    color: str | None = None
    context: dict[str, Any] | None = None


class BrandContextOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    data: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime


class BrandContextUpdate(BaseModel):
    data: dict[str, Any]


class KnowledgeItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID | None
    type: KnowledgeTypeT
    title: str
    body: str
    tags: list[str]
    viral_score: int | None
    pillar: PillarT | None
    source_file: str | None
    is_dormant: bool
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime


class KnowledgeItemCreate(BaseModel):
    type: KnowledgeTypeT
    title: str = Field(min_length=1, max_length=500)
    body: str
    project_id: uuid.UUID | None = None
    tags: list[str] = Field(default_factory=list)
    viral_score: int | None = Field(default=None, ge=0, le=20)
    pillar: PillarT | None = None
    source_file: str | None = None


class KnowledgeItemUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    body: str | None = None
    project_id: uuid.UUID | None = None
    tags: list[str] | None = None
    viral_score: int | None = Field(default=None, ge=0, le=20)
    pillar: PillarT | None = None
    is_dormant: bool | None = None
