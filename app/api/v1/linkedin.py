"""LinkedIn OAuth + account management endpoints."""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update

from app.api.deps import CurrentUser, DbSession
from app.config import settings
from app.database import SessionLocal
from app.models.linkedin import LinkedInAccount
from app.schemas.linkedin import (
    LinkedInAccountOut,
    LinkedInStartRequest,
    LinkedInStartResponse,
)
from app.services import linkedin_oauth, secrets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/linkedin", tags=["linkedin"])


# ---------------------------------------------------------------------------
# OAuth flow: start + callback
# ---------------------------------------------------------------------------


@router.post("/auth/start", response_model=LinkedInStartResponse)
async def start_oauth(
    payload: LinkedInStartRequest,
    current: CurrentUser,
) -> LinkedInStartResponse:
    """Begin the LinkedIn OAuth dance.

    Returns the consent URL the frontend should open in a popup/new tab.
    A signed state JWT carries the org_id + optional `redirect_after`
    target through the round-trip (LinkedIn echoes `state` back to our
    callback). See `services/linkedin_oauth.sign_state`.
    """
    if not settings.LINKEDIN_CLIENT_ID:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "LinkedIn OAuth не сконфигурирован на сервере (нет CLIENT_ID)",
        )

    state = linkedin_oauth.sign_state(
        organization_id=current.organization_id,
        redirect_after=str(payload.redirect_after) if payload.redirect_after else None,
    )
    url = linkedin_oauth.build_authorize_url(state=state)
    return LinkedInStartResponse(
        authorize_url=url,
        expires_in_seconds=linkedin_oauth.STATE_TTL_SECONDS,
    )


@router.get("/auth/callback")
async def oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """LinkedIn redirects here after the user grants (or denies) consent.

    No auth header on this request — the state JWT IS our auth. We:
      1. Validate `state` → extract org_id.
      2. If `error` present → redirect to the configured fallback with
         `?linkedin=error&reason=<error>`.
      3. Exchange `code` for tokens.
      4. Fetch user profile (sub, name, email, avatar).
      5. Upsert `linkedin_accounts` by (org_id, sub).
      6. Redirect back to `redirect_after` (or fallback) with
         `?linkedin=connected`.

    The frontend listens for the redirect via popup polling
    (`window.opener.location.search`) and refreshes the accounts list.
    """
    fallback = (
        settings.LINKEDIN_POST_CALLBACK_REDIRECT
        or (settings.cors_origins_list[0] if settings.cors_origins_list else "/")
    )

    # 1. State validation. If state itself is broken we can't even
    # determine which redirect to send the user to — fall back to the
    # server-side default.
    if not state:
        return _redirect_with_error(fallback, "missing_state")
    try:
        state_payload = linkedin_oauth.verify_state(state)
    except RuntimeError as exc:
        logger.warning("linkedin callback bad state: %s", exc)
        return _redirect_with_error(fallback, "bad_state")

    # Once we have the state, prefer its `redirect_after` (validated for
    # origin safety) for ALL further responses — error and success.
    desired_redirect = state_payload.get("redirect_after") or ""
    if desired_redirect and linkedin_oauth.is_safe_redirect(
        desired_redirect, allowed_origins=settings.cors_origins_list
    ):
        target = desired_redirect
    else:
        target = fallback

    # 2. User denied / LinkedIn refused — bounce with error info.
    if error:
        return _redirect_with_error(target, error)
    if not code:
        return _redirect_with_error(target, "missing_code")

    org_id = uuid.UUID(state_payload["org"])

    # 3. Exchange code → tokens
    try:
        token_data = await linkedin_oauth.exchange_code(code=code)
    except linkedin_oauth.OAuthExchangeFailed as exc:
        logger.warning("linkedin token exchange failed: %s", exc)
        return _redirect_with_error(target, "token_exchange_failed")

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = int(token_data.get("expires_in") or 0)
    refresh_expires_in = int(token_data.get("refresh_token_expires_in") or 0)
    scopes = linkedin_oauth.parse_scopes(token_data.get("scope"))

    if not access_token or expires_in <= 0:
        logger.warning("linkedin token response missing access_token / expires_in")
        return _redirect_with_error(target, "bad_token_response")

    # 4. Fetch user profile so the UI can show "Connected as Danil K."
    try:
        userinfo = await linkedin_oauth.fetch_userinfo(access_token=access_token)
    except linkedin_oauth.OAuthExchangeFailed as exc:
        logger.warning("linkedin userinfo fetch failed: %s", exc)
        return _redirect_with_error(target, "userinfo_failed")

    sub = userinfo.get("sub")
    if not sub:
        return _redirect_with_error(target, "missing_sub")
    display_name = userinfo.get("name") or userinfo.get("given_name") or "LinkedIn user"
    email = userinfo.get("email")
    avatar_url = userinfo.get("picture")

    # 5. Upsert. We use a fresh session here because this handler isn't
    # behind CurrentUser → the DbSession dep isn't injected.
    async with SessionLocal() as db:
        existing = await db.scalar(
            select(LinkedInAccount).where(
                LinkedInAccount.organization_id == org_id,
                LinkedInAccount.sub == sub,
            )
        )
        if existing is not None:
            existing.display_name = display_name
            existing.email = email
            existing.avatar_url = avatar_url
            existing.access_token_encrypted = secrets.encrypt(access_token) or ""
            if refresh_token:
                existing.refresh_token_encrypted = secrets.encrypt(refresh_token)
            existing.access_expires_at = linkedin_oauth.absolute_expiry(expires_in)
            if refresh_expires_in:
                existing.refresh_expires_at = linkedin_oauth.absolute_expiry(
                    refresh_expires_in
                )
            existing.scopes = scopes
        else:
            acc = LinkedInAccount(
                organization_id=org_id,
                sub=sub,
                display_name=display_name,
                email=email,
                avatar_url=avatar_url,
                access_token_encrypted=secrets.encrypt(access_token) or "",
                refresh_token_encrypted=secrets.encrypt(refresh_token),
                access_expires_at=linkedin_oauth.absolute_expiry(expires_in),
                refresh_expires_at=(
                    linkedin_oauth.absolute_expiry(refresh_expires_in)
                    if refresh_expires_in
                    else None
                ),
                scopes=scopes,
                is_default=False,
            )
            db.add(acc)
        await db.commit()

    # 6. Success — redirect back to the app with a hint flag so the
    # frontend toaster can confirm without an additional API call.
    return _redirect_with_success(target)


def _append_query(url: str, key: str, value: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{key}={value}"


def _redirect_with_error(url: str, reason: str) -> RedirectResponse:
    return RedirectResponse(
        url=_append_query(_append_query(url, "linkedin", "error"), "reason", reason),
        status_code=302,
    )


def _redirect_with_success(url: str) -> RedirectResponse:
    return RedirectResponse(
        url=_append_query(url, "linkedin", "connected"),
        status_code=302,
    )


# ---------------------------------------------------------------------------
# Account management
# ---------------------------------------------------------------------------


@router.get("/accounts", response_model=list[LinkedInAccountOut])
async def list_accounts(current: CurrentUser, db: DbSession) -> list[LinkedInAccountOut]:
    rows = await db.scalars(
        select(LinkedInAccount)
        .where(LinkedInAccount.organization_id == current.organization_id)
        .order_by(
            LinkedInAccount.is_default.desc(), LinkedInAccount.created_at.desc()
        )
    )
    return [LinkedInAccountOut.model_validate(r) for r in rows.all()]


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
):
    obj = await db.scalar(
        select(LinkedInAccount).where(
            LinkedInAccount.id == account_id,
            LinkedInAccount.organization_id == current.organization_id,
        )
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "LinkedIn account not found")
    await db.delete(obj)


@router.post("/accounts/{account_id}/set-default", response_model=LinkedInAccountOut)
async def set_default_account(
    account_id: uuid.UUID,
    current: CurrentUser,
    db: DbSession,
) -> LinkedInAccountOut:
    """Mark one account as the default for publishing.

    The publish flow (next sub-track) reads the default account when the
    user hits "Опубликовать в LinkedIn" without picking a specific
    identity. Mutually exclusive — picking a new default clears the flag
    on every other account in the same org in a single statement.
    """
    obj = await db.scalar(
        select(LinkedInAccount).where(
            LinkedInAccount.id == account_id,
            LinkedInAccount.organization_id == current.organization_id,
        )
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "LinkedIn account not found")
    await db.execute(
        update(LinkedInAccount)
        .where(
            LinkedInAccount.organization_id == current.organization_id,
            LinkedInAccount.id != obj.id,
        )
        .values(is_default=False)
    )
    obj.is_default = True
    await db.flush()
    return LinkedInAccountOut.model_validate(obj)
