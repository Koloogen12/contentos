"""Pydantic schemas for the LinkedIn OAuth integration.

Never exposes tokens — `LinkedInAccountOut` is the only client-facing
shape and it carries identity + expiry timestamps only. Token retrieval
for publishing goes through `services.linkedin_oauth.get_access_token`
on the server side; the frontend doesn't see raw tokens.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, HttpUrl


class LinkedInAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    sub: str
    display_name: str
    email: str | None
    avatar_url: str | None
    scopes: list[str]
    is_default: bool
    access_expires_at: datetime
    refresh_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class LinkedInStartRequest(BaseModel):
    """Optional `redirect_after` lets the frontend get sent back to the
    page it was on (e.g. a settings tab deep-link). Defaults to the
    server's `LINKEDIN_POST_CALLBACK_REDIRECT` setting. The URL must point
    to the same host as our app — we validate against CORS allowlist
    before redirecting to prevent open-redirect abuse."""

    redirect_after: HttpUrl | None = None


class LinkedInStartResponse(BaseModel):
    """The frontend opens `authorize_url` in a popup/new tab. After the
    user grants consent on LinkedIn they're redirected to our callback,
    which closes the popup (window.close) and notifies the opener tab via
    BroadcastChannel / window.opener.postMessage — the frontend listens
    and refreshes the accounts list."""

    authorize_url: str
    expires_in_seconds: int  # state-JWT TTL — frontend uses this to time out the popup
