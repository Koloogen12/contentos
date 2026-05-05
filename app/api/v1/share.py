"""Canvas sharing: create read-only public links + clone-from-share into your own org."""
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession
from app.models.auth import Organization
from app.models.canvas import Canvas, Edge, Node
from app.models.share import CanvasShareToken
from app.schemas.canvas import CanvasDetail, CanvasOut, EdgeOut, NodeOut
from app.schemas.share import (
    CanvasShareTokenCreated,
    CanvasShareTokenOut,
    PublicCanvasOut,
)

router = APIRouter(tags=["share"])


def _new_token() -> str:
    return secrets.token_urlsafe(24)


async def _owned_canvas(db, canvas_id: uuid.UUID, org_id: uuid.UUID) -> Canvas:
    canvas = await db.scalar(
        select(Canvas).where(Canvas.id == canvas_id, Canvas.organization_id == org_id)
    )
    if canvas is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Canvas not found")
    return canvas


# === Owner-side share management ===


@router.post(
    "/canvases/{canvas_id}/share",
    response_model=CanvasShareTokenCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_share_link(
    canvas_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> CanvasShareTokenCreated:
    canvas = await _owned_canvas(db, canvas_id, current.organization_id)
    token = _new_token()
    obj = CanvasShareToken(
        canvas_id=canvas.id,
        created_by_user_id=current.id,
        token=token,
        created_at=datetime.now(timezone.utc),
    )
    db.add(obj)
    await db.flush()
    return CanvasShareTokenCreated(id=obj.id, token=token, url_path=f"/p/{token}")


@router.get("/canvases/{canvas_id}/share-tokens", response_model=list[CanvasShareTokenOut])
async def list_share_tokens(
    canvas_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> list[CanvasShareTokenOut]:
    await _owned_canvas(db, canvas_id, current.organization_id)
    rows = await db.scalars(
        select(CanvasShareToken)
        .where(CanvasShareToken.canvas_id == canvas_id)
        .order_by(CanvasShareToken.created_at.desc())
    )
    return [CanvasShareTokenOut.model_validate(r) for r in rows.all()]


@router.delete(
    "/canvases/share-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_share_token(
    token_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> None:
    obj = await db.scalar(
        select(CanvasShareToken)
        .join(Canvas, Canvas.id == CanvasShareToken.canvas_id)
        .where(
            CanvasShareToken.id == token_id,
            Canvas.organization_id == current.organization_id,
        )
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share token not found")
    obj.revoked_at = datetime.now(timezone.utc)
    await db.flush()


# === Public read endpoint (no auth) ===


@router.get("/public/canvases/{token}", response_model=PublicCanvasOut)
async def get_public_canvas(token: str, db: DbSession) -> PublicCanvasOut:
    share = await db.scalar(
        select(CanvasShareToken).where(CanvasShareToken.token == token)
    )
    if share is None or share.revoked_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found or revoked")

    canvas = await db.scalar(
        select(Canvas)
        .where(Canvas.id == share.canvas_id)
        .options(selectinload(Canvas.nodes), selectinload(Canvas.edges))
    )
    if canvas is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Canvas not found")

    org = await db.scalar(
        select(Organization).where(Organization.id == canvas.organization_id)
    )

    return PublicCanvasOut(
        id=canvas.id,
        name=canvas.name,
        description=canvas.description,
        organization_name=org.name if org else "",
        nodes=[NodeOut.model_validate(n).model_dump(mode="json") for n in canvas.nodes],
        edges=[EdgeOut.model_validate(e).model_dump(mode="json") for e in canvas.edges],
        created_at=canvas.created_at,
    )


# === Clone a public canvas into your own org ===


class CloneFromShareRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    project_id: uuid.UUID | None = None


@router.post(
    "/canvases/from-share/{token}",
    response_model=CanvasDetail,
    status_code=status.HTTP_201_CREATED,
)
async def clone_from_share(
    token: str,
    payload: CloneFromShareRequest,
    current: CurrentUser,
    db: DbSession,
) -> CanvasDetail:
    """Cross-org clone via a share link. Runtime data preserved, status reset."""
    share = await db.scalar(
        select(CanvasShareToken).where(CanvasShareToken.token == token)
    )
    if share is None or share.revoked_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found or revoked")

    src = await db.scalar(
        select(Canvas)
        .where(Canvas.id == share.canvas_id)
        .options(selectinload(Canvas.nodes), selectinload(Canvas.edges))
    )
    if src is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source canvas not found")

    canvas = Canvas(
        organization_id=current.organization_id,
        project_id=payload.project_id,
        name=payload.name,
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
        **CanvasOut.model_validate(canvas).model_dump(),
        nodes=[NodeOut.model_validate(n) for n in canvas.nodes],
        edges=[EdgeOut.model_validate(e) for e in canvas.edges],
    )
