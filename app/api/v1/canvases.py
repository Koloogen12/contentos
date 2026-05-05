import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession
from app.models.canvas import Canvas
from app.schemas.canvas import (
    CanvasCreate,
    CanvasDetail,
    CanvasOut,
    CanvasUpdate,
    EdgeOut,
    NodeOut,
)

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
    return CanvasOut.model_validate(canvas)


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
    return [CanvasOut.model_validate(c) for c in result.all()]


@router.get("/templates", response_model=list[CanvasOut])
async def list_templates(current: CurrentUser, db: DbSession) -> list[CanvasOut]:
    stmt = (
        select(Canvas)
        .where(Canvas.organization_id == current.organization_id, Canvas.is_template.is_(True))
        .order_by(Canvas.updated_at.desc())
    )
    result = await db.scalars(stmt)
    return [CanvasOut.model_validate(c) for c in result.all()]


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
        **CanvasOut.model_validate(canvas).model_dump(),
        nodes=[NodeOut.model_validate(n) for n in canvas.nodes],
        edges=[EdgeOut.model_validate(e) for e in canvas.edges],
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
    return CanvasOut.model_validate(canvas)


@router.delete("/{canvas_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_canvas(canvas_id: uuid.UUID, current: CurrentUser, db: DbSession) -> None:
    canvas = await _get_owned_canvas(db, canvas_id, current.organization_id)
    await db.delete(canvas)


@router.post("/{canvas_id}/save-as-template", response_model=CanvasOut)
async def save_as_template(canvas_id: uuid.UUID, current: CurrentUser, db: DbSession) -> CanvasOut:
    canvas = await _get_owned_canvas(db, canvas_id, current.organization_id)
    canvas.is_template = True
    await db.flush()
    return CanvasOut.model_validate(canvas)
