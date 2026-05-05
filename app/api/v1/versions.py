"""Canvas version snapshots: history + restore."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession
from app.models.canvas import Canvas, Edge, Node
from app.models.version import CanvasVersion
from app.schemas.canvas import CanvasDetail, CanvasOut, EdgeOut, NodeOut
from app.schemas.version import CanvasVersionCreate, CanvasVersionDetail, CanvasVersionOut

router = APIRouter(tags=["versions"])


async def _owned_canvas(db, canvas_id: uuid.UUID, org_id: uuid.UUID) -> Canvas:
    canvas = await db.scalar(
        select(Canvas)
        .where(Canvas.id == canvas_id, Canvas.organization_id == org_id)
        .options(selectinload(Canvas.nodes), selectinload(Canvas.edges))
    )
    if canvas is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Canvas not found")
    return canvas


def _serialize_canvas(canvas: Canvas) -> dict:
    return {
        "name": canvas.name,
        "description": canvas.description,
        "project_id": str(canvas.project_id) if canvas.project_id else None,
        "nodes": [
            {
                "type": n.type,
                "position_x": n.position_x,
                "position_y": n.position_y,
                "data": dict(n.data or {}),
                "status": n.status,
                "_old_id": str(n.id),
            }
            for n in canvas.nodes
        ],
        "edges": [
            {
                "_source_old_id": str(e.source_node_id),
                "_target_old_id": str(e.target_node_id),
            }
            for e in canvas.edges
        ],
    }


@router.post(
    "/canvases/{canvas_id}/versions",
    response_model=CanvasVersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_version(
    canvas_id: uuid.UUID,
    payload: CanvasVersionCreate,
    current: CurrentUser,
    db: DbSession,
) -> CanvasVersionOut:
    canvas = await _owned_canvas(db, canvas_id, current.organization_id)
    snapshot = _serialize_canvas(canvas)
    version = CanvasVersion(
        canvas_id=canvas.id,
        created_by_user_id=current.id,
        label=payload.label,
        snapshot=snapshot,
        created_at=datetime.now(timezone.utc),
    )
    db.add(version)
    await db.flush()
    return CanvasVersionOut.model_validate(version)


@router.get("/canvases/{canvas_id}/versions", response_model=list[CanvasVersionOut])
async def list_versions(
    canvas_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> list[CanvasVersionOut]:
    await _owned_canvas(db, canvas_id, current.organization_id)
    rows = await db.scalars(
        select(CanvasVersion)
        .where(CanvasVersion.canvas_id == canvas_id)
        .order_by(CanvasVersion.created_at.desc())
    )
    return [CanvasVersionOut.model_validate(r) for r in rows.all()]


@router.get(
    "/canvases/{canvas_id}/versions/{version_id}",
    response_model=CanvasVersionDetail,
)
async def get_version(
    canvas_id: uuid.UUID,
    version_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
) -> CanvasVersionDetail:
    await _owned_canvas(db, canvas_id, current.organization_id)
    v = await db.scalar(
        select(CanvasVersion).where(
            CanvasVersion.id == version_id, CanvasVersion.canvas_id == canvas_id
        )
    )
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")
    return CanvasVersionDetail.model_validate(v)


@router.post(
    "/canvases/{canvas_id}/versions/{version_id}/restore",
    response_model=CanvasDetail,
)
async def restore_version(
    canvas_id: uuid.UUID,
    version_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
) -> CanvasDetail:
    """Replace the canvas's nodes and edges with the snapshot from this version.

    Before mutating, we save the *current* state as an auto-version so the user
    can roll forward again. The canvas's name/description are also restored
    if they were captured.
    """
    canvas = await _owned_canvas(db, canvas_id, current.organization_id)
    v = await db.scalar(
        select(CanvasVersion).where(
            CanvasVersion.id == version_id, CanvasVersion.canvas_id == canvas_id
        )
    )
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")

    # Auto-snapshot current state before destructive restore.
    auto = CanvasVersion(
        canvas_id=canvas.id,
        created_by_user_id=current.id,
        label="Авто-снимок до восстановления",
        snapshot=_serialize_canvas(canvas),
        created_at=datetime.now(timezone.utc),
    )
    db.add(auto)

    snapshot = v.snapshot or {}
    if "name" in snapshot and snapshot["name"]:
        canvas.name = snapshot["name"]
    if "description" in snapshot:
        canvas.description = snapshot["description"]

    # Wipe current nodes/edges (cascades will deal with skill_runs etc).
    for n in list(canvas.nodes):
        await db.delete(n)
    await db.flush()

    # Recreate nodes (assign new UUIDs) and remap edges via the snapshot's _old_id.
    new_id_map: dict[str, uuid.UUID] = {}
    for snap_n in snapshot.get("nodes", []):
        node = Node(
            canvas_id=canvas.id,
            type=snap_n["type"],
            position_x=snap_n.get("position_x", 0),
            position_y=snap_n.get("position_y", 0),
            data=dict(snap_n.get("data") or {}),
            status="idle",  # Reset runtime status on restore.
        )
        db.add(node)
        await db.flush()
        if old := snap_n.get("_old_id"):
            new_id_map[old] = node.id

    for snap_e in snapshot.get("edges", []):
        s = new_id_map.get(snap_e.get("_source_old_id"))
        t = new_id_map.get(snap_e.get("_target_old_id"))
        if s and t:
            db.add(Edge(canvas_id=canvas.id, source_node_id=s, target_node_id=t))
    await db.flush()

    canvas = await db.scalar(
        select(Canvas)
        .where(Canvas.id == canvas.id)
        .options(selectinload(Canvas.nodes), selectinload(Canvas.edges))
    )
    return CanvasDetail(
        **CanvasOut.model_validate(canvas).model_dump(),
        nodes=[NodeOut.model_validate(n) for n in canvas.nodes],
        edges=[EdgeOut.model_validate(e) for e in canvas.edges],
    )


@router.delete(
    "/canvases/{canvas_id}/versions/{version_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_version(
    canvas_id: uuid.UUID,
    version_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
) -> None:
    await _owned_canvas(db, canvas_id, current.organization_id)
    v = await db.scalar(
        select(CanvasVersion).where(
            CanvasVersion.id == version_id, CanvasVersion.canvas_id == canvas_id
        )
    )
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")
    await db.delete(v)
