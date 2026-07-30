"""linkedin_accounts table for OAuth-connected LinkedIn identities.

Revision ID: 0008_linkedin_accounts
Revises: 0007_telegram_metrics
Create Date: 2026-05-15 16:00:00.000000

LinkedIn OAuth (Sprint 2 Track C). One row per (org, LinkedIn user)
combination. We let an org connect multiple LinkedIn accounts so a team
can publish from several profiles (e.g. founder + cofounder); the
default-flag pattern matches `telegram_targets`.

Token fields are encrypted via `services/secrets.encrypt` before write
(same Fernet key as Telegram bot tokens). Plaintext access tokens never
hit disk.

A unique partial index over (org, sub) prevents duplicate rows for the
same LinkedIn identity inside one organization; multiple orgs CAN connect
the same LinkedIn user (rare, but legal).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_linkedin_accounts"
down_revision: str | None = "0007_telegram_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "linkedin_accounts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # `sub` is the LinkedIn OIDC subject identifier — stable per user.
        sa.Column("sub", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("avatar_url", sa.Text, nullable=True),
        sa.Column("access_token_encrypted", sa.Text, nullable=False),
        sa.Column("refresh_token_encrypted", sa.Text, nullable=True),
        sa.Column(
            "access_expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "refresh_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # Granted scopes (LinkedIn returns these from the token exchange).
        # Stored as a text array so we can branch on what's available
        # without re-parsing a space-separated string at every call site.
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.String),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "is_default",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "idx_linkedin_accounts_org",
        "linkedin_accounts",
        ["organization_id"],
    )
    op.create_index(
        "uq_linkedin_accounts_org_sub",
        "linkedin_accounts",
        ["organization_id", "sub"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_linkedin_accounts_org_sub", table_name="linkedin_accounts")
    op.drop_index("idx_linkedin_accounts_org", table_name="linkedin_accounts")
    op.drop_table("linkedin_accounts")
