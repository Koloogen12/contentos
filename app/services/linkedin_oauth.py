"""LinkedIn OAuth 2.0 — authorize URL, code exchange, token refresh, userinfo.

Why we don't use a third-party library: LinkedIn's OAuth surface is tiny
(three endpoints) and authlib / authlib-style frameworks pull in a
"provider registration" abstraction that's overkill for one provider. A
direct httpx call is shorter, easier to audit, and avoids a transitive
dependency we don't need elsewhere.

Endpoints (current as of 2026-05; LinkedIn versions these via host headers
not paths, so we don't need a x-restli-version pin for the OAuth flow):

  - authorize_url:  https://www.linkedin.com/oauth/v2/authorization
  - token_endpoint: https://www.linkedin.com/oauth/v2/accessToken
  - userinfo:       https://api.linkedin.com/v2/userinfo  (OIDC)

State token: a short-lived JWT signed with our `JWT_SECRET`, payload
contains the org_id, an optional redirect_after URL, a nonce, and a
discriminator type=`linkedin_state`. The discriminator stops an attacker
from replaying an access token as a state token (different `type` field).

CSRF: stateless — the cryptographic signature is the entire defence.
We don't need a server-side store; if the user opens two OAuth popups
in parallel they each get their own state JWT and both succeed.

Open redirect: `redirect_after` is validated against the CORS allowlist
before we 302 to it. Anything not in the allowlist falls back to the
configured `LINKEDIN_POST_CALLBACK_REDIRECT` (or the first CORS origin).
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import jwt

from app.config import settings

logger = logging.getLogger(__name__)


AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"


# State JWT TTL — long enough for the user to complete the LinkedIn consent
# screen, short enough that a leaked state can't be replayed days later.
# 10 minutes is the OAuth best-practice default.
STATE_TTL_SECONDS = 10 * 60

# Discriminator that prevents state-JWT confusion with access JWTs.
STATE_TOKEN_TYPE = "linkedin_state"


# ---------------------------------------------------------------------------
# State token (CSRF protection + redirect target carrier)
# ---------------------------------------------------------------------------


def sign_state(
    *,
    organization_id: uuid.UUID,
    redirect_after: str | None,
) -> str:
    """Issue a state JWT used in the OAuth round-trip.

    Carries the org so the callback (which arrives without an auth header)
    can identify which organization owns the new connection. Also carries
    `redirect_after` so the user lands back where they started (e.g. a
    settings tab) instead of always going to `/`.
    """
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "type": STATE_TOKEN_TYPE,
        "org": str(organization_id),
        "nonce": secrets.token_urlsafe(16),
        "iat": now,
        "exp": now + timedelta(seconds=STATE_TTL_SECONDS),
    }
    if redirect_after:
        payload["redirect_after"] = redirect_after
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def verify_state(state: str) -> dict[str, Any]:
    """Decode + validate a state JWT. Raises on any failure (caller maps
    to a 400). Returns the decoded payload."""
    try:
        payload = jwt.decode(
            state,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except jwt.PyJWTError as exc:
        raise RuntimeError(f"Invalid state token: {exc}") from exc
    if payload.get("type") != STATE_TOKEN_TYPE:
        raise RuntimeError("State token type mismatch")
    if "org" not in payload:
        raise RuntimeError("State token missing org claim")
    return payload


# ---------------------------------------------------------------------------
# Authorize URL
# ---------------------------------------------------------------------------


def build_authorize_url(*, state: str) -> str:
    """Construct the LinkedIn consent URL.

    Required query params (per LinkedIn OAuth v2 docs):
      response_type=code, client_id, redirect_uri, state, scope
    """
    if not settings.LINKEDIN_CLIENT_ID:
        raise RuntimeError("LINKEDIN_CLIENT_ID is empty — set it in .env")
    if not settings.LINKEDIN_REDIRECT_URI:
        raise RuntimeError("LINKEDIN_REDIRECT_URI is empty — set it in .env")

    params = {
        "response_type": "code",
        "client_id": settings.LINKEDIN_CLIENT_ID,
        "redirect_uri": settings.LINKEDIN_REDIRECT_URI,
        "state": state,
        "scope": settings.LINKEDIN_SCOPES,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Code exchange
# ---------------------------------------------------------------------------


class OAuthExchangeFailed(RuntimeError):
    """Wrapped LinkedIn token-endpoint error. Caller maps to 400/502."""


async def exchange_code(*, code: str) -> dict[str, Any]:
    """Trade an authorization code for access + refresh tokens.

    LinkedIn returns::

        {
          "access_token":  "<JWT-shaped opaque token>",
          "expires_in":    5184000,    // seconds (60 days)
          "refresh_token": "<token>",  // optional, only for apps with refresh_token_enabled
          "refresh_token_expires_in": 31536000,  // seconds (1 year)
          "scope":         "openid,profile,email,w_member_social",
          "token_type":    "Bearer"
        }
    """
    if not settings.LINKEDIN_CLIENT_SECRET:
        raise OAuthExchangeFailed("LINKEDIN_CLIENT_SECRET is empty — set it in .env")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": settings.LINKEDIN_CLIENT_ID,
        "client_secret": settings.LINKEDIN_CLIENT_SECRET,
        "redirect_uri": settings.LINKEDIN_REDIRECT_URI,
    }
    return await _post_token(data)


async def refresh_access_token(*, refresh_token: str) -> dict[str, Any]:
    """Exchange a refresh token for a fresh access token.

    Some LinkedIn apps don't have refresh_token grant enabled (it's a
    separate platform approval). When we get back a 4xx with
    `unsupported_grant_type` the caller should surface the error and ask
    the user to re-authorize from scratch.
    """
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": settings.LINKEDIN_CLIENT_ID,
        "client_secret": settings.LINKEDIN_CLIENT_SECRET,
    }
    return await _post_token(data)


async def _post_token(data: dict[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            TOKEN_URL,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
    if r.status_code != 200:
        # LinkedIn error bodies look like
        #   {"error": "invalid_request", "error_description": "...."}
        # We don't surface the description to the user (could leak the
        # redirect URI or client_id) — log it and return a generic msg.
        body = r.text[:500]
        logger.warning("linkedin token endpoint %s: %s", r.status_code, body)
        raise OAuthExchangeFailed(
            f"LinkedIn token exchange failed ({r.status_code})"
        )
    return r.json()


# ---------------------------------------------------------------------------
# Userinfo
# ---------------------------------------------------------------------------


async def fetch_userinfo(*, access_token: str) -> dict[str, Any]:
    """Pull OIDC claims for the authenticated LinkedIn user.

    Endpoint returns::

        {
          "sub":    "AbCdEf123",
          "name":   "Danil Kochnev",
          "given_name":  "Danil",
          "family_name": "Kochnev",
          "picture": "https://media.licdn.com/.../profile.jpg",
          "email":   "danil@example.com",
          "email_verified": true,
          "locale":  {"country": "RU", "language": "ru"}
        }

    Requires the `openid profile email` scopes. `picture`/`email` are
    absent when those scopes are not granted; we tolerate their absence.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            USERINFO_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
    if r.status_code != 200:
        logger.warning("linkedin userinfo %s: %s", r.status_code, r.text[:300])
        raise OAuthExchangeFailed(
            f"LinkedIn userinfo failed ({r.status_code})"
        )
    return r.json()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_scopes(raw: str | None) -> list[str]:
    """Normalise LinkedIn's space-or-comma separated scope strings.

    LinkedIn returns `scope` as `"openid,profile,email,w_member_social"`
    on some token responses and `"openid profile email w_member_social"`
    on others. Normalise to a clean list.
    """
    if not raw:
        return []
    parts = raw.replace(",", " ").split()
    return [p.strip() for p in parts if p.strip()]


def absolute_expiry(seconds_from_now: int) -> datetime:
    """Convert LinkedIn's `expires_in` (relative seconds) to an absolute
    timezone-aware UTC datetime suitable for the DB column."""
    return datetime.now(timezone.utc) + timedelta(
        seconds=max(0, int(seconds_from_now))
    )


def is_safe_redirect(url: str, *, allowed_origins: list[str]) -> bool:
    """Open-redirect guard: only allow redirects to origins on the allow-list.

    `allowed_origins` is typically `settings.cors_origins_list`. We compare
    by (scheme, host, port) — paths and query strings are unrestricted.
    A bare hostname mismatch returns False so we fall back to the default
    redirect target rather than 302-ing to attacker-controlled domains.
    """
    if not url:
        return False
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if u.scheme not in ("http", "https"):
        return False
    incoming = (u.scheme, u.hostname, u.port)
    for origin in allowed_origins:
        try:
            o = urlparse(origin)
        except ValueError:
            continue
        if (o.scheme, o.hostname, o.port) == incoming:
            return True
    return False
