"""project.brand_visual JSONB for visual carousel rendering settings

Revision ID: 0006_project_brand_visual
Revises: 0005_edge_data
Create Date: 2026-05-15 12:00:00.000000

Adds a `brand_visual` JSONB column on `projects` so the carousel renderer
can pick up the project's display preferences without an extra round-trip
to a separate settings table. Fields (all optional, all unset by default):

    username         — str, e.g. "kochnefff" — shown in slide header
    avatar_url       — str | null — avatar image overlay (Phase 2, ignored now)
    show_numbering   — bool, default true — "3/7" in slide top-right
    prepend_number   — bool, default true — "1. Title" vs just "Title"
    eyebrow_text     — str, default "ОШИБКА" — eyebrow tag for body slides
    style            — str, default "editorial_dark" — visual style id

These map 1:1 to the `BrandVisual` dataclass in
`app/services/render/carousel.py`. Adding more fields later is a no-op
since this is a JSONB document.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_project_brand_visual"
down_revision: str | None = "0005_edge_data"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "brand_visual",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("projects", "brand_visual")
