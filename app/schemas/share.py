import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CanvasShareTokenOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    canvas_id: uuid.UUID
    token: str
    created_by_user_id: uuid.UUID
    created_at: datetime
    revoked_at: datetime | None


class CanvasShareTokenCreated(BaseModel):
    id: uuid.UUID
    token: str
    url_path: str  # `/p/{token}` — фронт строит абсолютный URL


class PublicCanvasOut(BaseModel):
    """Read-only canvas snapshot served without auth at /api/v1/public/canvases/{token}."""

    id: uuid.UUID
    name: str
    description: str | None
    organization_name: str
    nodes: list[dict]
    edges: list[dict]
    created_at: datetime
