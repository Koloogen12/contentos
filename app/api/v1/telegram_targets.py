import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select, update

from app.api.deps import CurrentUser, DbSession
from app.models.publish import TelegramTarget
from app.schemas.publish import TelegramTargetCreate, TelegramTargetOut, TelegramTargetUpdate
from app.services import secrets

router = APIRouter(prefix="/telegram-targets", tags=["telegram-targets"])


async def _owned(db, target_id: uuid.UUID, org_id: uuid.UUID) -> TelegramTarget:
    obj = await db.scalar(
        select(TelegramTarget).where(
            TelegramTarget.id == target_id, TelegramTarget.organization_id == org_id
        )
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Target not found")
    return obj


async def _clear_other_defaults(db, org_id: uuid.UUID, except_id: uuid.UUID | None) -> None:
    stmt = update(TelegramTarget).where(TelegramTarget.organization_id == org_id)
    if except_id is not None:
        stmt = stmt.where(TelegramTarget.id != except_id)
    await db.execute(stmt.values(is_default=False))


@router.get("", response_model=list[TelegramTargetOut])
async def list_targets(current: CurrentUser, db: DbSession) -> list[TelegramTargetOut]:
    rows = await db.scalars(
        select(TelegramTarget)
        .where(TelegramTarget.organization_id == current.organization_id)
        .order_by(TelegramTarget.is_default.desc(), TelegramTarget.created_at.desc())
    )
    return [TelegramTargetOut.model_validate(r) for r in rows.all()]


@router.post("", response_model=TelegramTargetOut, status_code=status.HTTP_201_CREATED)
async def create_target(
    payload: TelegramTargetCreate, current: CurrentUser, db: DbSession
) -> TelegramTargetOut:
    if payload.is_default:
        await _clear_other_defaults(db, current.organization_id, except_id=None)

    obj = TelegramTarget(
        organization_id=current.organization_id,
        title=payload.title,
        chat_id=payload.chat_id,
        bot_token_encrypted=secrets.encrypt(payload.bot_token),
        is_default=payload.is_default,
    )
    db.add(obj)
    await db.flush()
    return TelegramTargetOut.model_validate(obj)


@router.patch("/{target_id}", response_model=TelegramTargetOut)
async def update_target(
    target_id: uuid.UUID,
    payload: TelegramTargetUpdate,
    current: CurrentUser,
    db: DbSession,
) -> TelegramTargetOut:
    obj = await _owned(db, target_id, current.organization_id)

    data = payload.model_dump(exclude_unset=True)
    if "bot_token" in data:
        new_token = data.pop("bot_token")
        # Empty string on edit means "keep existing token" per the frontend
        # contract; only overwrite when a real value is supplied.
        if new_token:
            obj.bot_token_encrypted = secrets.encrypt(new_token)

    if data.get("is_default") is True:
        await _clear_other_defaults(db, current.organization_id, except_id=obj.id)

    for field, value in data.items():
        setattr(obj, field, value)
    await db.flush()
    return TelegramTargetOut.model_validate(obj)


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target(target_id: uuid.UUID, current: CurrentUser, db: DbSession) -> None:
    obj = await _owned(db, target_id, current.organization_id)
    await db.delete(obj)
