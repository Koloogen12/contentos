import uuid
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.auth import User
from app.services.auth import decode_token

bearer_scheme = HTTPBearer(auto_error=True)

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def _user_from_access_token(db: AsyncSession, token: str) -> User:
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired") from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc

    if payload.get("type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong token type")

    user_id_str = payload.get("sub")
    if not user_id_str:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing subject")

    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad subject") from exc

    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or disabled")
    return user


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    db: DbSession,
) -> User:
    return await _user_from_access_token(db, credentials.credentials)


async def get_current_user_query_or_header(
    db: DbSession,
    token: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
) -> User:
    """Auth from EITHER `?token=` OR `Authorization: Bearer ...`.

    The query-param path exists for EventSource (SSE) clients in the browser,
    which cannot set custom headers. Treat the token as a regular access JWT.
    """
    raw = token
    if not raw and authorization:
        if authorization.lower().startswith("bearer "):
            raw = authorization[7:].strip()
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing token")
    return await _user_from_access_token(db, raw)


CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentUserSse = Annotated[User, Depends(get_current_user_query_or_header)]
