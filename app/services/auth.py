import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from passlib.context import CryptContext

from app.config import settings

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
_SLUG_CLEAN = re.compile(r"[^a-z0-9]+")


def hash_password(plain: str) -> str:
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)


def make_org_slug(seed: str) -> str:
    base = _SLUG_CLEAN.sub("-", seed.lower()).strip("-") or "org"
    return f"{base[:32]}-{uuid.uuid4().hex[:6]}"


def _encode(payload: dict[str, Any], ttl: timedelta) -> str:
    now = datetime.now(UTC)
    payload = {**payload, "iat": now, "exp": now + ttl}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def make_access_token(*, user_id: uuid.UUID, organization_id: uuid.UUID) -> str:
    return _encode(
        {
            "sub": str(user_id),
            "org": str(organization_id),
            "type": "access",
        },
        timedelta(minutes=settings.JWT_ACCESS_TTL_MINUTES),
    )


def make_refresh_token(*, user_id: uuid.UUID) -> str:
    return _encode(
        {"sub": str(user_id), "type": "refresh"},
        timedelta(days=settings.JWT_REFRESH_TTL_DAYS),
    )


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
