import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.canvas import Canvas, Node
from app.schemas.canvas import NodeCreate, NodeOut, NodeUpdate, canvas_to_out, edge_to_out, node_to_out

router = APIRouter(tags=["nodes"])


async def _get_owned_node(db, node_id: uuid.UUID, org_id: uuid.UUID) -> Node:
    node = await db.scalar(
        select(Node).join(Canvas).where(Node.id == node_id, Canvas.organization_id == org_id)
    )
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Node not found")
    return node


@router.post("/canvases/{canvas_id}/nodes", response_model=NodeOut, status_code=status.HTTP_201_CREATED)
async def create_node(
    canvas_id: uuid.UUID,
    payload: NodeCreate,
    current: CurrentUser,
    db: DbSession,
) -> NodeOut:
    canvas = await db.scalar(
        select(Canvas).where(Canvas.id == canvas_id, Canvas.organization_id == current.organization_id)
    )
    if canvas is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Canvas not found")
    node = Node(
        canvas_id=canvas.id,
        type=payload.type,
        position_x=payload.position_x,
        position_y=payload.position_y,
        data=payload.data,
    )
    db.add(node)
    await db.flush()
    return node_to_out(node)


@router.patch("/nodes/{node_id}", response_model=NodeOut)
async def update_node(
    node_id: uuid.UUID,
    payload: NodeUpdate,
    current: CurrentUser,
    db: DbSession,
) -> NodeOut:
    node = await _get_owned_node(db, node_id, current.organization_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(node, field, value)
    await db.flush()
    return node_to_out(node)


@router.delete("/nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node(node_id: uuid.UUID, current: CurrentUser, db: DbSession) -> None:
    node = await _get_owned_node(db, node_id, current.organization_id)
    await db.delete(node)
