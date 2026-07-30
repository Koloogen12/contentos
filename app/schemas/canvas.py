import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

NodeTypeT = Literal["source", "extract", "format", "llm"]
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
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class EdgeCreate(BaseModel):
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    # Optional per-edge metadata. Today we use it for {"tezis_index": int}
    # when spawning a format node from a specific talking point card.
    data: dict[str, Any] = Field(default_factory=dict)


class EdgeUpdate(BaseModel):
    data: dict[str, Any] | None = None


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


# Manual constructors avoid Pydantic from_attributes triggering lazy
# relationship loads in an async session (the MissingGreenlet error).
# Use these instead of `*Out.model_validate(orm_obj)` after a flush.

def node_to_out(node) -> "NodeOut":
    return NodeOut(
        id=node.id,
        canvas_id=node.canvas_id,
        type=node.type,
        position_x=node.position_x,
        position_y=node.position_y,
        data=node.data or {},
        status=node.status,
        created_at=node.created_at,
        updated_at=node.updated_at,
    )


def edge_to_out(edge) -> "EdgeOut":
    return EdgeOut(
        id=edge.id,
        canvas_id=edge.canvas_id,
        source_node_id=edge.source_node_id,
        target_node_id=edge.target_node_id,
        data=edge.data or {},
        created_at=edge.created_at,
    )


def canvas_to_out(canvas) -> "CanvasOut":
    return CanvasOut(
        id=canvas.id,
        organization_id=canvas.organization_id,
        project_id=canvas.project_id,
        name=canvas.name,
        description=canvas.description,
        is_template=canvas.is_template,
        created_at=canvas.created_at,
        updated_at=canvas.updated_at,
    )
