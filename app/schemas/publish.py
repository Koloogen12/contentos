import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PublishStatusT = Literal["pending", "sending", "sent", "failed"]


class TelegramTargetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    title: str
    chat_id: str
    is_default: bool
    created_at: datetime
    updated_at: datetime


class TelegramTargetCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    chat_id: str = Field(min_length=1, max_length=64)
    bot_token: str | None = None
    is_default: bool = False


class TelegramTargetUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    chat_id: str | None = Field(default=None, min_length=1, max_length=64)
    bot_token: str | None = None
    is_default: bool | None = None


class PublishStart(BaseModel):
    target_id: uuid.UUID


class PublishStarted(BaseModel):
    publish_log_id: uuid.UUID
    status: PublishStatusT


class PublishLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    node_id: uuid.UUID
    target_id: uuid.UUID
    status: PublishStatusT
    text: str
    response: dict[str, Any] | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None
