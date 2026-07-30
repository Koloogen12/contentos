"""Anonymous trial flow — organizations.kind + trial usage counters

Revision ID: 0009_trial_orgs
Revises: 0008_linkedin_accounts
Create Date: 2026-05-15 17:00:00.000000

Adds trial-mode columns to `organizations` so anonymous visitors get a
real Org row with hard usage limits, instead of a separate "anon session"
shadow table. The unified shape means every downstream query (canvas,
node, knowledge, voice_sample) keeps working unchanged — there's just a
quota gate in the AI-run endpoints and a nightly cleanup cron for
expired trials.

Fields:

  - `kind`: 'regular' | 'trial'. Regular orgs are the existing path
    (registered users + paid plans). Trial orgs are anonymous sessions
    that auto-expire.

  - `trial_expires_at`: when the 24h trial window ends. After this the
    user can't run new AI tasks, but the org still exists for the
    "convert to permanent" registration flow (until trial_expires_at +
    24h grace, then cleanup cron deletes the row + cascades).

  - `trial_ai_runs_used`: hard counter incremented on every AI-run
    enqueue. Compared against a constant cap in code (5 for strict
    tier). NOT reset on conversion — once you used your trial, you
    used it.

  - `trial_renders_used`: separate counter for visual carousel renders
    because they're expensive ($0.04 per cover + Playwright). 1 render
    is allowed per trial.

  - `trial_session_id`: optional fingerprint for abuse tracking. Cookie
    or IP-derived. Not used for auth (JWT does that) — just for the
    rate-limit-by-source story when we add Cloudflare Turnstile later.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_trial_orgs"
down_revision: str | None = "0008_linkedin_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "kind",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'regular'"),
        ),
    )
    op.create_check_constraint(
        "ck_organizations_kind",
        "organizations",
        "kind IN ('regular', 'trial')",
    )
    op.add_column(
        "organizations",
        sa.Column(
            "trial_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "trial_ai_runs_used",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "trial_renders_used",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "organizations",
        sa.Column("trial_session_id", sa.String(64), nullable=True),
    )
    # Partial index so the cleanup cron can find expired trials cheaply.
    op.create_index(
        "idx_organizations_trial_cleanup",
        "organizations",
        ["trial_expires_at"],
        postgresql_where=sa.text("kind = 'trial'"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_organizations_trial_cleanup", table_name="organizations"
    )
    op.drop_column("organizations", "trial_session_id")
    op.drop_column("organizations", "trial_renders_used")
    op.drop_column("organizations", "trial_ai_runs_used")
    op.drop_column("organizations", "trial_expires_at")
    op.drop_constraint("ck_organizations_kind", "organizations")
    op.drop_column("organizations", "kind")
