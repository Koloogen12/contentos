"""planned_posts.platform: allow 'vc'

Revision ID: 0014_vc_platform
Revises: 0013_review_platform
Create Date: 2026-07-30 18:00:00.000000

Формат «материал для vc.ru» (services/skills/vc_creator.py). Без этого
поставить его в контент-план нельзя: CHECK перечисляет форматы поимённо.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0014_vc_platform"
down_revision: str | None = "0013_review_platform"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "'telegram','instagram','linkedin','twitter','article','carousel','reels','hooks','review'"
_NEW = _OLD + ",'vc'"


def upgrade() -> None:
    op.drop_constraint("ck_planned_posts_platform", "planned_posts", type_="check")
    op.create_check_constraint(
        "ck_planned_posts_platform", "planned_posts", f"platform IN ({_NEW})"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM planned_posts WHERE platform = 'vc') THEN
                RAISE EXCEPTION 'Cannot downgrade: % planned post(s) use platform=vc.',
                    (SELECT count(*) FROM planned_posts WHERE platform = 'vc');
            END IF;
        END $$;
        """
    )
    op.drop_constraint("ck_planned_posts_platform", "planned_posts", type_="check")
    op.create_check_constraint(
        "ck_planned_posts_platform", "planned_posts", f"platform IN ({_OLD})"
    )
