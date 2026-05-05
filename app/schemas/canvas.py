import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

NodeTypeT = Literal["source", "extract", "format"]
NodeStatusT = Literal["idle", "running", "done", "error"]


class NodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    canvas_id: uuid.UUID
    type: NodeTypeT
    position_x: float
    position_y: float
    data: dict[str, Any]
    status: NodeStatusT
    created_at: datetime
    updated_at: datetime


class NodeCreate(BaseModel):
    type: NodeTypeT
    position_x: float = 0
    position_y: float = 0
    data: dict[str, Any] = Field(default_factory=dict)


class NodeUpdate(BaseModel):
    position_x: float | None = None
    position_y: float | None = None
    data: dict[str, Any] | None = None
    status: NodeStatusT | None = None


class EdgeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    canvas_id: uuid.UUID
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    created_at: datetime


class EdgeCreate(BaseModel):
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID


class CanvasOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID | None
    name: str
    description: str | None
    is_template: bool
    created_at: datetime
    updated_at: datetime


class CanvasDetail(CanvasOut):
    nodes: list[NodeOut]
    edges: list[EdgeOut]


class CanvasCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    project_id: uuid.UUID | None = None


class CanvasUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    project_id: uuid.UUID | None = None
