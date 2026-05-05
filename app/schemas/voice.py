import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class VoiceSampleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID | None
    platform: str | None
    text: str
    meta: dict[str, Any]
    has_embedding: bool
    created_at: datetime
    updated_at: datetime


class VoiceSampleCreate(BaseModel):
    text: str = Field(min_length=20)
    platform: str | None = None
    project_id: uuid.UUID | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class VoiceSampleBulkCreate(BaseModel):
    samples: list[VoiceSampleCreate]


class VoiceSampleBulkResult(BaseModel):
    created: int
    skipped: int
    items: list[VoiceSampleOut]


class VoiceTraitsExtracted(BaseModel):
    voice_traits: list[str]
    voice_avoid: list[str]
    recurring_phrases: list[str]
    tone_calibration: str
    samples_analyzed: int
