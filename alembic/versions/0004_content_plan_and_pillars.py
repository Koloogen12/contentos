"""content plan + pillars on knowledge_items + manifesto type

Revision ID: 0004_content_plan_and_pillars
Revises: 0003_canvas_versions
Create Date: 2026-05-06 03:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_content_plan_and_pillars"
down_revision: str | None = "0003_canvas_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- planned_posts ---
    op.create_table(
        "planned_posts",
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
        sa.Column(
            "canvas_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("canvases.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("platform", sa.String(50), nullable=False),
        sa.Column("hook", sa.Text(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("cta", sa.Text(), nullable=False, server_default=""),
        sa.Column("full_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("talking_point_text", sa.Text(), nullable=True),
        sa.Column("scheduled_date", sa.Date(), nullable=True),
        sa.Column("scheduled_time", sa.Time(), nullable=True),
        sa.Column(
            "status", sa.String(50), nullable=False, server_default="draft"
        ),
        sa.Column("pillar", sa.String(10), nullable=True),
        sa.Column(
            "tags", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metrics",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "platform IN ('telegram','instagram','linkedin','twitter','article','carousel','reels','hooks')",
            name="ck_planned_posts_platform",
        ),
        sa.CheckConstraint(
            "status IN ('draft','ready','scheduled','published','skipped')",
            name="ck_planned_posts_status",
        ),
        sa.CheckConstraint(
            "pillar IS NULL OR pillar IN ('R1','R2','R3','R4')",
            name="ck_planned_posts_pillar",
        ),
    )
    op.create_index(
        "idx_planned_posts_org", "planned_posts", ["organization_id"]
    )
    op.create_index(
        "idx_planned_posts_date", "planned_posts", ["scheduled_date"]
    )
    op.create_index("idx_planned_posts_status", "planned_posts", ["status"])
    op.create_index(
        "idx_planned_posts_platform", "planned_posts", ["platform"]
    )
    op.create_index("idx_planned_posts_pillar", "planned_posts", ["pillar"])

    # --- knowledge_items: add 'manifesto' type + pillar column ---
    op.drop_constraint("ck_knowledge_items_type", "knowledge_items", type_="check")
    op.create_check_constraint(
        "ck_knowledge_items_type",
        "knowledge_items",
        "type IN ('tezis','reference','audience','voice_rule','content_theme','manifesto')",
    )
    op.add_column(
        "knowledge_items",
        sa.Column("pillar", sa.String(10), nullable=True),
    )
    op.create_check_constraint(
        "ck_knowledge_items_pillar",
        "knowledge_items",
        "pillar IS NULL OR pillar IN ('R1','R2','R3','R4')",
    )
    op.create_index(
        "idx_knowledge_pillar", "knowledge_items", ["pillar"]
    )


def downgrade() -> None:
    op.drop_index("idx_knowledge_pillar", "knowledge_items")
    op.drop_constraint("ck_knowledge_items_pillar", "knowledge_items", type_="check")
    op.drop_column("knowledge_items", "pillar")
    op.drop_constraint("ck_knowledge_items_type", "knowledge_items", type_="check")
    op.create_check_constraint(
        "ck_knowledge_items_type",
        "knowledge_items",
        "type IN ('tezis','reference','audience','voice_rule','content_theme')",
    )

    op.drop_index("idx_planned_posts_pillar", "planned_posts")
    op.drop_index("idx_planned_posts_platform", "planned_posts")
    op.drop_index("idx_planned_posts_status", "planned_posts")
    op.drop_index("idx_planned_posts_date", "planned_posts")
    op.drop_index("idx_planned_posts_org", "planned_posts")
    op.drop_table("planned_posts")
