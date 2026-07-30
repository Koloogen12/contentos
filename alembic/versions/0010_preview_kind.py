"""organizations.kind: add 'preview' value + relax constraint

Revision ID: 0010_preview_kind
Revises: 0009_trial_orgs
Create Date: 2026-05-15 19:00:00.000000

Architectural rework of the trial flow:

  Before: anonymous visitor → kind='trial' with 24h timer → register
  After:  anonymous visitor → kind='preview' (no timer, hard caps)
          → mandatory register → kind='trial' (24h timer starts now)
          → trial expires → kind='regular' (Phase 2 paid plans gate ops)

Migration safety: existing trial rows (the few test sessions Danil
created during smoke-tests) get mapped to 'preview' since they're all
anonymous (synthetic emails). Real registered users with `kind='regular'`
are untouched.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_preview_kind"
down_revision: str | None = "0009_trial_orgs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Drop the old constraint (allows only regular | trial).
    op.drop_constraint("ck_organizations_kind", "organizations")
    # 2. Re-add it with the new allowed set.
    op.create_check_constraint(
        "ck_organizations_kind",
        "organizations",
        "kind IN ('preview', 'trial', 'regular')",
    )
    # 3. Migrate existing trial rows → preview (they were all anonymous).
    op.execute("UPDATE organizations SET kind = 'preview' WHERE kind = 'trial'")


def downgrade() -> None:
    # Revert preview → trial so the old constraint passes.
    op.execute("UPDATE organizations SET kind = 'trial' WHERE kind = 'preview'")
    op.drop_constraint("ck_organizations_kind", "organizations")
    op.create_check_constraint(
        "ck_organizations_kind",
        "organizations",
        "kind IN ('regular', 'trial')",
    )
