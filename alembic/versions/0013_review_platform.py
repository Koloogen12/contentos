"""planned_posts.platform: allow 'review'

Revision ID: 0013_review_platform
Revises: 0012_email_verify_yandex
Create Date: 2026-07-27 16:00:00.000000

Новый формат «рецензия на материал» (см. services/skills/review_creator.py).
Без этого поставить рецензию в контент-план нельзя: CHECK на platform
перечисляет форматы поимённо и падает на незнакомом значении.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0013_review_platform"
down_revision: str | None = "0012_email_verify_yandex"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PLATFORMS_NEW = (
    "'telegram','instagram','linkedin','twitter','article','carousel','reels','hooks','review'"
)
_PLATFORMS_OLD = (
    "'telegram','instagram','linkedin','twitter','article','carousel','reels','hooks'"
)


def upgrade() -> None:
    op.drop_constraint("ck_planned_posts_platform", "planned_posts", type_="check")
    op.create_check_constraint(
        "ck_planned_posts_platform",
        "planned_posts",
        f"platform IN ({_PLATFORMS_NEW})",
    )


def downgrade() -> None:
    # Строки с platform='review' не пролезут в старый CHECK. Молча удалять
    # чужие посты нельзя, поэтому падаем с понятным сообщением.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM planned_posts WHERE platform = 'review') THEN
                RAISE EXCEPTION
                    'Cannot downgrade: % planned post(s) use platform=review.',
                    (SELECT count(*) FROM planned_posts WHERE platform = 'review');
            END IF;
        END $$;
        """
    )
    op.drop_constraint("ck_planned_posts_platform", "planned_posts", type_="check")
    op.create_check_constraint(
        "ck_planned_posts_platform",
        "planned_posts",
        f"platform IN ({_PLATFORMS_OLD})",
    )
