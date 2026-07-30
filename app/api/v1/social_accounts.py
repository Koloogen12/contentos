"""Подключение аккаунтов площадок, работающих через внешний шлюз.

Telegram и LinkedIn подключаются своими ручками (у них свой поток и свои
таблицы) — здесь Instagram, Threads и X.

Поток: клиент просит ссылку → пользователь авторизуется на площадке →
шлюз возвращает его обратно → мы синхронизируем список аккаунтов. Своего
callback-эндпоинта нет намеренно: сверять состояние по редиректу
ненадёжно, потому что пользователь может закрыть вкладку на полпути.
Источник правды — список аккаунтов у шлюза, и мы всегда спрашиваем его.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.config import settings
from app.models.auth import Organization
from app.models.social import SocialAccount
from app.services import publishing, zernio

router = APIRouter(prefix="/social-accounts", tags=["social-accounts"])


class SocialAccountOut(BaseModel):
    id: uuid.UUID
    provider: str
    platform: str
    display_name: str
    username: str | None = None
    avatar_url: str | None = None
    is_default: bool
    is_active: bool


class ConnectUrlOut(BaseModel):
    authorize_url: str


def _to_out(a: SocialAccount) -> SocialAccountOut:
    return SocialAccountOut(
        id=a.id,
        provider=a.provider,
        platform=a.platform,
        display_name=a.display_name,
        username=a.username,
        avatar_url=a.avatar_url,
        is_default=a.is_default,
        is_active=a.is_active,
    )


async def _ensure_profile(db, org: Organization) -> str:
    """Профиль-арендатор на стороне шлюза, по одному на организацию.

    Создаётся лениво — при первом подключении площадки: заводить профиль
    каждой регистрации значило бы дёргать чужой сервис на всех, включая
    тех, кто публикацией никогда не воспользуется.
    """
    if org.zernio_profile_id:
        return org.zernio_profile_id

    profile_id = await zernio.create_profile(name=org.name or str(org.id))
    org.zernio_profile_id = profile_id
    await db.flush()
    return profile_id


@router.get("", response_model=list[SocialAccountOut])
async def list_accounts(
    current: CurrentUser, db: DbSession
) -> list[SocialAccountOut]:
    rows = await db.scalars(
        select(SocialAccount)
        .where(SocialAccount.organization_id == current.organization_id)
        .order_by(SocialAccount.created_at)
    )
    return [_to_out(r) for r in rows.all()]


@router.post("/connect/{platform}", response_model=ConnectUrlOut)
async def connect(
    platform: str, current: CurrentUser, db: DbSession
) -> ConnectUrlOut:
    if platform not in publishing.GATEWAY_PLATFORMS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"«{platform}» подключается не через шлюз",
        )
    if not zernio.is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Публикация через шлюз пока не настроена на сервере.",
        )

    org = await db.scalar(
        select(Organization).where(Organization.id == current.organization_id)
    )
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")

    try:
        profile_id = await _ensure_profile(db, org)
        front = (settings.PUBLIC_URL_FRONT or "").split(",")[0].strip()
        url = await zernio.connect_url(
            platform=platform,
            profile_id=profile_id,
            redirect_url=f"{front}/connections?connected={platform}",
        )
    except zernio.ZernioError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return ConnectUrlOut(authorize_url=url)


@router.post("/sync", response_model=list[SocialAccountOut])
async def sync(current: CurrentUser, db: DbSession) -> list[SocialAccountOut]:
    """Привести локальный список в соответствие с состоянием у шлюза.

    Вызывается после возврата с авторизации и по кнопке «обновить».
    Отключённое на стороне площадки помечается неактивным, а не удаляется:
    у публикаций остаются ссылки на аккаунт, и по ним должно быть видно,
    куда пост уходил.
    """
    if not zernio.is_configured():
        return await list_accounts(current, db)

    org = await db.scalar(
        select(Organization).where(Organization.id == current.organization_id)
    )
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")

    profile_id = org.zernio_profile_id
    if not profile_id:
        return await list_accounts(current, db)

    try:
        remote = await zernio.list_accounts(str(profile_id))
    except zernio.ZernioError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    rows = list(
        (
            await db.scalars(
                select(SocialAccount).where(
                    SocialAccount.organization_id == current.organization_id,
                    SocialAccount.provider == "zernio",
                )
            )
        ).all()
    )
    by_external = {r.external_id: r for r in rows}
    seen: set[str] = set()

    for item in remote:
        external_id = str(item.get("_id") or item.get("id") or "")
        platform = str(item.get("platform") or "")
        if not external_id or platform not in publishing.GATEWAY_PLATFORMS:
            continue
        seen.add(external_id)
        name = str(
            item.get("displayName") or item.get("name") or item.get("username") or platform
        )
        row = by_external.get(external_id)
        if row is None:
            row = SocialAccount(
                organization_id=current.organization_id,
                provider="zernio",
                platform=platform,
                external_id=external_id,
                external_profile_id=str(profile_id),
                display_name=name,
            )
            db.add(row)
        row.display_name = name
        row.username = item.get("username")
        row.avatar_url = item.get("profileImageUrl") or item.get("avatarUrl")
        row.is_active = True
        row.meta = item

    for row in rows:
        if row.external_id not in seen:
            row.is_active = False

    await db.flush()
    return await list_accounts(current, db)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(account_id: uuid.UUID, current: CurrentUser, db: DbSession):
    row = await db.scalar(
        select(SocialAccount).where(
            SocialAccount.id == account_id,
            SocialAccount.organization_id == current.organization_id,
        )
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    # Отключаем у шлюза в первую очередь: пока аккаунт числится
    # подключённым там, он тарифицируется, даже если у нас его уже нет.
    if row.provider == "zernio" and zernio.is_configured():
        try:
            await zernio.disconnect_account(row.external_id)
        except zernio.ZernioError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    await db.delete(row)
