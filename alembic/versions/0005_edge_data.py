"""edge.data JSONB for per-edge metadata (tezis_index)

Revision ID: 0005_edge_data
Revises: 0004_content_plan_and_pillars
Create Date: 2026-05-10 22:00:00.000000

Adds a `data` JSONB column on `edges` so a format node can record which
talking_point of its parent extract node it was spawned from. Without this
the extract node's `selected_index` was the only signal — meaning every
format node descendant resolved to the same tezis. Now each edge carries
its own pointer ({"tezis_index": N}) and `collect_input_for_skill` prefers
it over the parent's selection.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_edge_data"
down_revision: str | None = "0004_content_plan_and_pillars"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "edges",
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("edges", "data")
