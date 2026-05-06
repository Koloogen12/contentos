import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.canvas import Canvas, Edge, Node
from app.schemas.canvas import EdgeCreate, EdgeOut, canvas_to_out, edge_to_out, node_to_out

router = APIRouter(tags=["edges"])

_ALLOWED = {("source", "extract"), ("extract", "format"), ("source", "format")}


@router.post("/canvases/{canvas_id}/edges", response_model=EdgeOut, status_code=status.HTTP_201_CREATED)
async def create_edge(
    canvas_id: uuid.UUID,
    payload: EdgeCreate,
    current: CurrentUser,
    db: DbSession,
) -> EdgeOut:
    canvas = await db.scalar(
        select(Canvas).where(Canvas.id == canvas_id, Canvas.organization_id == current.organization_id)
    )
    if canvas is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Canvas not found")

    src = await db.scalar(select(Node).where(Node.id == payload.source_node_id, Node.canvas_id == canvas.id))
    tgt = await db.scalar(select(Node).where(Node.id == payload.target_node_id, Node.canvas_id == canvas.id))
    if src is None or tgt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source or target node not in canvas")

    if (src.type, tgt.type) not in _ALLOWED:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Edge {src.type} → {tgt.type} not allowed",
        )

    edge = Edge(canvas_id=canvas.id, source_node_id=src.id, target_node_id=tgt.id)
    db.add(edge)
    try:
        await db.flush()
    except Exception as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Edge already exists") from exc
    return edge_to_out(edge)


@router.delete("/edges/{edge_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_edge(edge_id: uuid.UUID, current: CurrentUser, db: DbSession) -> None:
    edge = await db.scalar(
        select(Edge).join(Canvas).where(Edge.id == edge_id, Canvas.organization_id == current.organization_id)
    )
    if edge is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Edge not found")
    await db.delete(edge)
