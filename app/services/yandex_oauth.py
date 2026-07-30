"""Yandex OAuth — «войти через Яндекс».

Differs from `linkedin_oauth` in purpose: LinkedIn connects a publishing
identity to an *existing* workspace, this one authenticates a person and may
create the workspace. So the state token carries no organization — there isn't
one yet at that point.

Docs: https://yandex.ru/dev/id/doc/ru/
  authorize  GET  https://oauth.yandex.ru/authorize
  token      POST https://oauth.yandex.ru/token
  identity   GET  https://login.yandex.ru/info
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from app.config import settings

AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
TOKEN_URL = "https://oauth.yandex.ru/token"
USERINFO_URL = "https://login.yandex.ru/info"

# Long enough to sign in on Yandex (including a password manager and 2FA
# detour), short enough that a leaked state cannot be replayed later.
STATE_TTL_SECONDS = 900

# Discriminator so a state JWT can never be presented as an access token,
# and vice versa — both are signed with the same secret.
STATE_TOKEN_TYPE = "yandex_state"


def configured() -> bool:
    return bool(
        settings.YANDEX_CLIENT_ID
        and settings.YANDEX_CLIENT_SECRET
        and settings.YANDEX_REDIRECT_URI
    )


# ---------------------------------------------------------------------------
# State token (CSRF protection + redirect target carrier)
# ---------------------------------------------------------------------------


def sign_state(*, redirect_after: str | None) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "type": STATE_TOKEN_TYPE,
        "nonce": secrets.token_urlsafe(16),
        "iat": now,
        "exp": now + timedelta(seconds=STATE_TTL_SECONDS),
    }
    if redirect_after:
        payload["redirect_after"] = redirect_after
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def verify_state(state: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            state, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
    except jwt.PyJWTError as exc:
        raise RuntimeError(f"Invalid state token: {exc}") from exc
    if payload.get("type") != STATE_TOKEN_TYPE:
        raise RuntimeError("State token type mismatch")
    return payload


def build_authorize_url(*, state: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.YANDEX_CLIENT_ID,
            "redirect_uri": settings.YANDEX_REDIRECT_URI,
            "state": state,
            # Yandex takes the permission list from the app registration, not
            # from the request, so no `scope` here. The app must have
            # "login:email" and "login:info" ticked.
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


# ---------------------------------------------------------------------------
# Token exchange + identity
# ---------------------------------------------------------------------------


async def exchange_code(*, code: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": settings.YANDEX_CLIENT_ID,
                "client_secret": settings.YANDEX_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if response.status_code != 200:
        raise RuntimeError(f"Yandex token exchange failed: {response.status_code} {response.text}")
    return response.json()


async def fetch_userinfo(*, access_token: str) -> dict[str, Any]:
    """Identity of the signed-in Yandex account.

    Returns the raw payload; `extract_identity` picks what we need out of it.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            USERINFO_URL,
            params={"format": "json"},
            headers={"Authorization": f"OAuth {access_token}"},
        )
    if response.status_code != 200:
        raise RuntimeError(f"Yandex userinfo failed: {response.status_code} {response.text}")
    return response.json()


def extract_identity(info: dict[str, Any]) -> tuple[str, str, str | None]:
    """(yandex_user_id, email, display_name) from a `login.yandex.ru/info` payload.

    An account can hide its address — the permission is grantable separately —
    and then there is nothing to key a workspace on. Better to say so than to
    invent a synthetic address the person can never receive mail at.
    """
    yandex_id = str(info.get("id") or "").strip()
    if not yandex_id:
        raise RuntimeError("Yandex userinfo has no id")

    email = (info.get("default_email") or "").strip()
    if not email:
        emails = info.get("emails") or []
        email = (emails[0] if emails else "").strip()
    if not email:
        raise RuntimeError("no_email")

    name = (info.get("real_name") or info.get("display_name") or "").strip() or None
    return yandex_id, email.lower(), name
