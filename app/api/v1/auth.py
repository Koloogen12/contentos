import uuid

import jwt
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.auth import Organization, User
from app.models.knowledge import BrandContext
from app.schemas.auth import (
    LoginRequest,
    MeResponse,
    OrganizationOut,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserOut,
)
from app.services.auth import (
    decode_token,
    hash_password,
    make_access_token,
    make_org_slug,
    make_refresh_token,
    verify_password,
)
from app.services.templates_seed import seed_default_templates

router = APIRouter(prefix="/auth", tags=["auth"])


DEFAULT_BRAND_CONTEXT: dict = {
    "author_name": "",
    "author_handle": "",
    "author_bio": "",
    "active_products": "",
    "voice_rules": (
        "Серьёзный по умолчанию. Юмор вспышками. Абсурд ОК, клоунада нет. "
        "Короткие фразы. Иногда длинные — чтобы разложить. Потом снова короткое. "
        "Маркеры: «по факту», «по сути», «короче», «оч», «фишка в том», «мощно». "
        "Без эмодзи. Без многоточий — точки вместо них."
    ),
    "taboo_list": (
        "Личная жизнь, жена, отношения. Реальные долги публично. "
        "Underdog-нарратив (из грязи в князи). Корпоративный язык: синергия, "
        "инновационное решение. Агрессивные продажи. Кликбейт без содержания. "
        "Гуру-позиция (я знаю как правильно жить). Хвастовство деньгами, машинами, "
        "люксом. Политика РФ."
    ),
    "manifesto": (
        "Ты больше, чем думаешь.\n"
        "Состояние порождает действия. Сначала состояние — потом результат.\n"
        "Люди нанимают продукт на работу, не на фичу.\n"
        "AI — твой лучший кофаундер. Можешь стать солопренером.\n"
        "Для успешного продукта инвестиции не нужны. Нужна формула.\n"
        "Всё всегда изнутри наружу. Состояние → мысль → действие → мир → деньги."
    ),
    "cta_keywords": ["СТЕК", "ВОПРОСЫ", "РАБОТА", "СЕГМЕНТ", "ПРОВЕРКА", "ABCD"],
    "content_pillars": {
        "R1": "Как создавать продукт (JTBD, MVP, AI-инструменты) — 40%",
        "R2": "Психология фаундера (страхи, состояние, знать ≠ делать) — 20%",
        "R3": "Реалити «Путь к $1M MRR» (THE MONO, NEURIN AI) — 25%",
        "R4": "Разборы венчурных сделок — 15%",
    },
}


@router.post("/register", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: DbSession) -> TokenPair:
    existing = await db.scalar(select(User).where(User.email == payload.email))
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    org_seed = payload.organization_name or payload.email.split("@")[0]
    org = Organization(
        name=payload.organization_name or f"{payload.email.split('@')[0]}'s workspace",
        slug=make_org_slug(org_seed),
    )
    db.add(org)
    await db.flush()

    user = User(
        organization_id=org.id,
        email=payload.email,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
    )
    db.add(user)

    db.add(BrandContext(organization_id=org.id, data=DEFAULT_BRAND_CONTEXT))
    await db.flush()

    await seed_default_templates(db, org.id)

    return TokenPair(
        access_token=make_access_token(user_id=user.id, organization_id=org.id),
        refresh_token=make_refresh_token(user_id=user.id),
    )


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, db: DbSession) -> TokenPair:
    user = await db.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account disabled")
    return TokenPair(
        access_token=make_access_token(user_id=user.id, organization_id=user.organization_id),
        refresh_token=make_refresh_token(user_id=user.id),
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, db: DbSession) -> TokenPair:
    try:
        decoded = decode_token(payload.refresh_token)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token expired") from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token") from exc

    if decoded.get("type") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong token type")

    user = await db.scalar(select(User).where(User.id == uuid.UUID(decoded["sub"])))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User unavailable")

    return TokenPair(
        access_token=make_access_token(user_id=user.id, organization_id=user.organization_id),
        refresh_token=make_refresh_token(user_id=user.id),
    )


@router.get("/me", response_model=MeResponse)
async def me(current: CurrentUser, db: DbSession) -> MeResponse:
    org = await db.scalar(select(Organization).where(Organization.id == current.organization_id))
    assert org is not None
    return MeResponse(
        user=UserOut.model_validate(current),
        organization=OrganizationOut.model_validate(org),
    )
