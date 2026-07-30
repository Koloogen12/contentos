"""Org-state machine: preview → trial → regular.

Three kinds of organizations:

  - **preview** (`kind='preview'`): anonymous visitor's sandbox.
    Created when someone hits `/auth/preview-session` without auth.
    No timer. Hard-capped to 1 canvas, 3 AI ops, 1 carousel render.
    After they finish their first Format-node, the frontend pops a
    MANDATORY register modal — there's no "Позже" path.

  - **trial** (`kind='trial'`): registered user inside the 24h trial
    window. Created by `/auth/register-preview` (preview → trial
    conversion) or by `/auth/register` for new direct signups.
    NO operation caps within the 24h window — full feature access so
    the user can validate the product end-to-end with their real
    workflow.

  - **regular** (`kind='regular'`): post-trial / paid plan. Phase 2
    will gate feature access via a Plan FK; today regular = unlimited.

Counters (`trial_ai_runs_used`, `trial_renders_used`) are kept across
all kinds for analytics & quota gating, but they only matter while
`kind='preview'`. Trial users have time-based gating only; regular
users aren't gated by us at all (yet).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Final

from fastapi import HTTPException, status
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import Organization


# Preview caps. Tight enough to keep API spend < $0.05 per visitor
# (≈ 3 text completions @ gpt-5.4 + 1 gpt-image-2 cover + Playwright).
PREVIEW_MAX_AI_RUNS: Final[int] = 3
PREVIEW_MAX_RENDERS: Final[int] = 1
PREVIEW_MAX_CANVASES: Final[int] = 1

# Trial window. Starts ticking the moment the user converts preview →
# registered. Within the window: unlimited AI ops / renders / canvases
# (no per-trial cost cap — high-intent users can fully evaluate the
# product). After expiry: kind flips to 'regular' and Phase 2's Plan
# system takes over quota.
TRIAL_WINDOW_HOURS: Final[int] = 24
# Grace period for cleanup cron: don't auto-delete TRIAL orgs even
# after expiry. Trial = registered user, deletion would lose their
# data. The cleanup cron only deletes never-converted PREVIEW orgs.
PREVIEW_CLEANUP_DAYS: Final[int] = 7


def is_preview(org: Organization) -> bool:
    return org.kind == "preview"


def is_trial(org: Organization) -> bool:
    return org.kind == "trial"


def is_regular(org: Organization) -> bool:
    return org.kind == "regular"


def trial_seconds_remaining(org: Organization) -> int:
    """Seconds until trial expiry. Returns 0 for non-trial orgs or
    expired trials."""
    if org.kind != "trial" or not org.trial_expires_at:
        return 0
    return max(
        0,
        int((org.trial_expires_at - datetime.now(timezone.utc)).total_seconds()),
    )


def trial_is_expired(org: Organization) -> bool:
    """True when kind='trial' AND time has run out. Used to gate AI ops
    after the trial window — the org stays as `kind='trial'` until the
    cleanup cron flips it to 'regular' (so the frontend can show an
    "upgrade" CTA based on this state)."""
    return (
        org.kind == "trial"
        and org.trial_expires_at is not None
        and org.trial_expires_at < datetime.now(timezone.utc)
    )


def make_trial_window() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TRIAL_WINDOW_HOURS)


def preview_remaining(org: Organization) -> dict[str, int]:
    """Per-quota counts for the preview badge UI."""
    return {
        "ai_runs": max(0, PREVIEW_MAX_AI_RUNS - (org.trial_ai_runs_used or 0)),
        "renders": max(0, PREVIEW_MAX_RENDERS - (org.trial_renders_used or 0)),
    }


# ---------------------------------------------------------------------------
# Quota gates
# ---------------------------------------------------------------------------


async def assert_ai_quota(db: AsyncSession, org: Organization) -> None:
    """Raise 402 if the org is over its AI-op limit for its kind.

    - preview: hard cap (PREVIEW_MAX_AI_RUNS)
    - trial:   no cap, but blocked if trial_expires_at is in the past
    - regular: no-op (Phase 2's Plan layer will gate this)
    """
    if is_preview(org):
        if (org.trial_ai_runs_used or 0) >= PREVIEW_MAX_AI_RUNS:
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED,
                f"Превью: исчерпан лимит ({PREVIEW_MAX_AI_RUNS} AI-запусков). "
                "Зарегистрируйся, чтобы получить 24-часовой триал без лимитов.",
            )
        return
    if is_trial(org):
        if trial_is_expired(org):
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED,
                "Триал-период истёк. Подключи тариф, чтобы продолжить.",
            )
        return
    # regular: no quota check yet (Phase 2 plan-based)


async def assert_render_quota(db: AsyncSession, org: Organization) -> None:
    """Same as `assert_ai_quota` but for visual carousel renders.

    Preview has its own (much tighter) render cap because the cover-image
    generation costs $0.04 per call. Trial gets unlimited renders within
    the 24h window. Regular: no-op for now.
    """
    if is_preview(org):
        if (org.trial_renders_used or 0) >= PREVIEW_MAX_RENDERS:
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED,
                f"Превью: исчерпан лимит ({PREVIEW_MAX_RENDERS} рендер). "
                "Зарегистрируйся, чтобы получить 24-часовой триал без лимитов.",
            )
        return
    if is_trial(org):
        if trial_is_expired(org):
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED,
                "Триал-период истёк. Подключи тариф, чтобы продолжить.",
            )
        return


async def assert_canvas_quota(
    db: AsyncSession, org: Organization, current_count: int
) -> None:
    if is_preview(org) and current_count >= PREVIEW_MAX_CANVASES:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            f"Превью: только {PREVIEW_MAX_CANVASES} канвас. "
            "Зарегистрируйся, чтобы создавать неограниченно.",
        )


# ---------------------------------------------------------------------------
# Counter increments
# ---------------------------------------------------------------------------


async def incr_ai_usage(db: AsyncSession, organization_id: uuid.UUID) -> None:
    """Bump the AI-runs counter for preview orgs. Trial/regular orgs
    aren't gated by this counter, so we no-op them via the WHERE
    clause (saves a useless write)."""
    await db.execute(
        update(Organization)
        .where(Organization.id == organization_id, Organization.kind == "preview")
        .values(trial_ai_runs_used=Organization.trial_ai_runs_used + 1)
    )


async def incr_render_usage(db: AsyncSession, organization_id: uuid.UUID) -> None:
    await db.execute(
        update(Organization)
        .where(Organization.id == organization_id, Organization.kind == "preview")
        .values(trial_renders_used=Organization.trial_renders_used + 1)
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def preview_user_email_placeholder(session_id: str) -> str:
    """Synthetic email for anonymous preview users.

    User.email has a UniqueConstraint, so we need SOMETHING. The user
    converts to a real email via `/auth/register-preview` — at which
    point this placeholder is overwritten.
    """
    return f"preview-{session_id}@preview.contentos.local"


# Legacy alias — older code paths used this name.
trial_user_email_placeholder = preview_user_email_placeholder
