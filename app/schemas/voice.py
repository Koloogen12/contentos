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


class RedpolitikaDraft(BaseModel):
    """Черновик редполитики: читатель, площадки, регистр, словарь, фактура.

    Всё, что модель смогла вывести из образцов, приходит заполненным; всё,
    чего в текстах не видно, — пустым и перечислено в `gaps`. Пустое поле
    здесь честнее выдуманного: редполитика с придуманным читателем хуже,
    чем редполитика без него, потому что ей начинают доверять.
    """

    reader: str = ""
    platforms: dict[str, str] = Field(default_factory=dict)
    register: str = ""
    register_exceptions: str = ""
    lexicon_forbidden: list[str] = Field(default_factory=list)
    lexicon_required: list[str] = Field(default_factory=list)
    evidence_base: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    samples_analyzed: int = 0


class RedpolitikaUpdate(BaseModel):
    """То, что пользователь подтвердил или поправил в черновике."""

    reader: str = ""
    platforms: dict[str, str] = Field(default_factory=dict)
    register: str = ""
    register_exceptions: str = ""
    lexicon_forbidden: list[str] = Field(default_factory=list)
    lexicon_required: list[str] = Field(default_factory=list)
    evidence_base: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Auto-import (Telegram public channel / YouTube channel / blog URLs)
# ---------------------------------------------------------------------------

class TelegramImportRequest(BaseModel):
    """Pull last N posts from a public Telegram channel via web-view."""
    handle: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=50, ge=1, le=100)
    project_id: uuid.UUID | None = None


class YoutubeImportRequest(BaseModel):
    """Pull last N video transcripts from a public YouTube channel."""
    channel: str = Field(min_length=1, max_length=255)
    limit: int = Field(default=10, ge=1, le=30)
    project_id: uuid.UUID | None = None


class UrlImportRequest(BaseModel):
    """Pull the main article body from one or more public blog URLs."""
    urls: list[str] = Field(min_length=1, max_length=20)
    project_id: uuid.UUID | None = None


class VoiceImportResult(BaseModel):
    """Common response shape across all three import endpoints."""
    source: str  # "telegram" | "youtube" | "url"
    created: int
    skipped: int
    items: list[VoiceSampleOut]
    notes: list[str] = Field(default_factory=list)
