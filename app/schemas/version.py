import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CanvasVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    canvas_id: uuid.UUID
    created_by_user_id: uuid.UUID
    label: str | None
    created_at: datetime


class CanvasVersionDetail(CanvasVersionOut):
    snapshot: dict[str, Any]


class CanvasVersionCreate(BaseModel):
    label: str | None = Field(default=None, max_length=255)
