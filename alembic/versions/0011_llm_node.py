"""nodes.type: add 'llm' value + relax constraint

Revision ID: 0011_llm_node
Revises: 0010_preview_kind
Create Date: 2026-07-17 12:00:00.000000

Adds a new node type `llm` — a chat node that holds a conversation with an
LLM (Opus 4.8 via CometAPI) and pulls context from whatever nodes are wired
into it. No data migration needed: existing nodes keep their types; the
constraint just gains one allowed value.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0011_llm_node"
down_revision: str | None = "0010_preview_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_nodes_type", "nodes")
    op.create_check_constraint(
        "ck_nodes_type",
        "nodes",
        "type IN ('source', 'extract', 'format', 'llm')",
    )


def downgrade() -> None:
    # Drop any llm nodes first so the stricter constraint re-applies cleanly.
    op.execute("DELETE FROM nodes WHERE type = 'llm'")
    op.drop_constraint("ck_nodes_type", "nodes")
    op.create_check_constraint(
        "ck_nodes_type",
        "nodes",
        "type IN ('source', 'extract', 'format')",
    )
