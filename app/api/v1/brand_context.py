from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.knowledge import BrandContext
from app.schemas.knowledge import BrandContextOut, BrandContextUpdate

router = APIRouter(prefix="/brand-context", tags=["brand-context"])


@router.get("", response_model=BrandContextOut)
async def get_brand_context(current: CurrentUser, db: DbSession) -> BrandContextOut:
    obj = await db.scalar(
        select(BrandContext).where(BrandContext.organization_id == current.organization_id)
    )
    if obj is None:
        obj = BrandContext(organization_id=current.organization_id, data={}, version=1)
        db.add(obj)
        await db.flush()
    return BrandContextOut.model_validate(obj)


@router.put("", response_model=BrandContextOut)
async def update_brand_context(
    payload: BrandContextUpdate, current: CurrentUser, db: DbSession
) -> BrandContextOut:
    obj = await db.scalar(
        select(BrandContext).where(BrandContext.organization_id == current.organization_id)
    )
    if obj is None:
        obj = BrandContext(organization_id=current.organization_id, data=payload.data, version=1)
        db.add(obj)
    else:
        obj.data = payload.data
        obj.version = (obj.version or 1) + 1
    await db.flush()
    return BrandContextOut.model_validate(obj)
