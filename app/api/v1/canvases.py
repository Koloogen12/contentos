import uuid

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession
from app.models.canvas import Canvas, Edge, Node
from app.schemas.canvas import (
    canvas_to_out,
    edge_to_out,
    node_to_out,
    CanvasCreate,
    CanvasDetail,
    CanvasOut,
    CanvasUpdate,
    EdgeOut,
    NodeOut,
)


class CanvasFromTemplate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    project_id: uuid.UUID | None = None

router = APIRouter(prefix="/canvases", tags=["canvases"])


async def _get_owned_canvas(db, canvas_id: uuid.UUID, org_id: uuid.UUID) -> Canvas:
    canvas = await db.scalar(
        select(Canvas).where(Canvas.id == canvas_id, Canvas.organization_id == org_id)
    )
    if canvas is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Canvas not found")
    return canvas


@router.post("", response_model=CanvasOut, status_code=status.HTTP_201_CREATED)
async def create_canvas(payload: CanvasCreate, current: CurrentUser, db: DbSession) -> CanvasOut:
    canvas = Canvas(
        organization_id=current.organization_id,
        project_id=payload.project_id,
        name=payload.name,
        description=payload.description,
    )
    db.add(canvas)
    await db.flush()
    return canvas_to_out(canvas)


@router.get("", response_model=list[CanvasOut])
async def list_canvases(
    current: CurrentUser,
    db: DbSession,
    project_id: uuid.UUID | None = Query(default=None),
    is_template: bool | None = Query(default=None),
) -> list[CanvasOut]:
    stmt = select(Canvas).where(Canvas.organization_id == current.organization_id)
    if project_id is not None:
        stmt = stmt.where(Canvas.project_id == project_id)
    if is_template is not None:
        stmt = stmt.where(Canvas.is_template == is_template)
    stmt = stmt.order_by(Canvas.updated_at.desc())
    result = await db.scalars(stmt)
    return [canvas_to_out(c) for c in result.all()]


@router.get("/templates", response_model=list[CanvasOut])
async def list_templates(current: CurrentUser, db: DbSession) -> list[CanvasOut]:
    stmt = (
        select(Canvas)
        .where(Canvas.organization_id == current.organization_id, Canvas.is_template.is_(True))
        .order_by(Canvas.updated_at.desc())
    )
    result = await db.scalars(stmt)
    return [canvas_to_out(c) for c in result.all()]


@router.get("/{canvas_id}", response_model=CanvasDetail)
async def get_canvas(canvas_id: uuid.UUID, current: CurrentUser, db: DbSession) -> CanvasDetail:
    canvas = await db.scalar(
        select(Canvas)
        .where(Canvas.id == canvas_id, Canvas.organization_id == current.organization_id)
        .options(selectinload(Canvas.nodes), selectinload(Canvas.edges))
    )
    if canvas is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Canvas not found")
    return CanvasDetail(
        **canvas_to_out(canvas).model_dump(),
        nodes=[node_to_out(n) for n in canvas.nodes],
        edges=[edge_to_out(e) for e in canvas.edges],
    )


@router.patch("/{canvas_id}", response_model=CanvasOut)
async def update_canvas(
    canvas_id: uuid.UUID,
    payload: CanvasUpdate,
    current: CurrentUser,
    db: DbSession,
) -> CanvasOut:
    canvas = await _get_owned_canvas(db, canvas_id, current.organization_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(canvas, field, value)
    await db.flush()
    return canvas_to_out(canvas)


@router.delete("/{canvas_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_canvas(canvas_id: uuid.UUID, current: CurrentUser, db: DbSession) -> None:
    canvas = await _get_owned_canvas(db, canvas_id, current.organization_id)
    await db.delete(canvas)


@router.post("/{canvas_id}/save-as-template", response_model=CanvasOut)
async def save_as_template(canvas_id: uuid.UUID, current: CurrentUser, db: DbSession) -> CanvasOut:
    canvas = await _get_owned_canvas(db, canvas_id, current.organization_id)
    canvas.is_template = True
    await db.flush()
    return canvas_to_out(canvas)


@router.post(
    "/from-template/{template_id}",
    response_model=CanvasDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_from_template(
    template_id: uuid.UUID,
    payload: CanvasFromTemplate,
    current: CurrentUser,
    db: DbSession,
) -> CanvasDetail:
    """Clone a template (its nodes and edges) into a fresh user canvas.

    The template must be owned by the same org. We don't carry over any
    runtime data — node.data is reset to {} (or to a minimal seed kept on
    the template node) and node.status is reset to 'idle'.
    """
    template = await db.scalar(
        select(Canvas)
        .where(
            Canvas.id == template_id,
            Canvas.organization_id == current.organization_id,
            Canvas.is_template.is_(True),
        )
        .options(selectinload(Canvas.nodes), selectinload(Canvas.edges))
    )
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found")

    canvas = Canvas(
        organization_id=current.organization_id,
        project_id=payload.project_id,
        name=payload.name,
        description=template.description,
        is_template=False,
    )
    db.add(canvas)
    await db.flush()

    node_id_map: dict[uuid.UUID, uuid.UUID] = {}
    for src in template.nodes:
        copied = Node(
            canvas_id=canvas.id,
            type=src.type,
            position_x=src.position_x,
            position_y=src.position_y,
            data=_clean_template_data(src.type, src.data or {}),
            status="idle",
        )
        db.add(copied)
        await db.flush()
        node_id_map[src.id] = copied.id

    for e in template.edges:
        new_source = node_id_map.get(e.source_node_id)
        new_target = node_id_map.get(e.target_node_id)
        if new_source and new_target:
            db.add(
                Edge(
                    canvas_id=canvas.id,
                    source_node_id=new_source,
                    target_node_id=new_target,
                )
            )
    await db.flush()

    canvas = await db.scalar(
        select(Canvas)
        .where(Canvas.id == canvas.id)
        .options(selectinload(Canvas.nodes), selectinload(Canvas.edges))
    )
    return CanvasDetail(
        **canvas_to_out(canvas).model_dump(),
        nodes=[node_to_out(n) for n in canvas.nodes],
        edges=[edge_to_out(e) for e in canvas.edges],
    )


_TEMPLATE_KEEP_FIELDS = {
    "source": {"input_type", "platform", "notes"},
    "extract": set(),
    "format": {"platform"},
}


def _clean_template_data(node_type: str, data: dict) -> dict:
    """Strip runtime fields from a template node before cloning."""
    keep = _TEMPLATE_KEEP_FIELDS.get(node_type, set())
    return {k: v for k, v in data.items() if k in keep}


@router.post(
    "/{canvas_id}/duplicate",
    response_model=CanvasDetail,
    status_code=status.HTTP_201_CREATED,
)
async def duplicate_canvas(
    canvas_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
) -> CanvasDetail:
    """Clone an existing user canvas (NOT a template).

    Unlike from-template, runtime data is preserved verbatim — the user
    is duplicating a working pipeline, not starting fresh. The clone
    starts as a non-template owned by the same project (if any).
    """
    src = await db.scalar(
        select(Canvas)
        .where(Canvas.id == canvas_id, Canvas.organization_id == current.organization_id)
        .options(selectinload(Canvas.nodes), selectinload(Canvas.edges))
    )
    if src is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Canvas not found")

    canvas = Canvas(
        organization_id=current.organization_id,
        project_id=src.project_id,
        name=f"{src.name} (копия)",
        description=src.description,
        is_template=False,
    )
    db.add(canvas)
    await db.flush()

    node_id_map: dict[uuid.UUID, uuid.UUID] = {}
    for n in src.nodes:
        copied = Node(
            canvas_id=canvas.id,
            type=n.type,
            position_x=n.position_x,
            position_y=n.position_y,
            data=dict(n.data or {}),
            status="idle",
        )
        db.add(copied)
        await db.flush()
        node_id_map[n.id] = copied.id

    for e in src.edges:
        if (s := node_id_map.get(e.source_node_id)) and (t := node_id_map.get(e.target_node_id)):
            db.add(Edge(canvas_id=canvas.id, source_node_id=s, target_node_id=t))
    await db.flush()

    canvas = await db.scalar(
        select(Canvas)
        .where(Canvas.id == canvas.id)
        .options(selectinload(Canvas.nodes), selectinload(Canvas.edges))
    )
    return CanvasDetail(
        **canvas_to_out(canvas).model_dump(),
        nodes=[node_to_out(n) for n in canvas.nodes],
        edges=[edge_to_out(e) for e in canvas.edges],
    )
