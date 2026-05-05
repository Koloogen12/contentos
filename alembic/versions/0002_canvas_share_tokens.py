"""canvas share tokens

Revision ID: 0002_canvas_share_tokens
Revises: 0001_initial
Create Date: 2026-05-05 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_canvas_share_tokens"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "canvas_share_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "canvas_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("canvases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_canvas_share_tokens_canvas", "canvas_share_tokens", ["canvas_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_canvas_share_tokens_canvas", "canvas_share_tokens")
    op.drop_table("canvas_share_tokens")
