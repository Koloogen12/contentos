"""social_accounts + publish_logs.social_account_id

Публикация умела только Telegram: `publish_logs.target_id` ссылался прямо
на `telegram_targets`, то есть поставщик был зашит в схему. Здесь появляется
общая таблица подключённых аккаунтов, а лог получает вторую, необязательную
ссылку на неё.

Обе ссылки сделаны nullable, и старая не удаляется: существующие записи о
телеграм-публикациях должны остаться читаемыми. Проверка на уровне БД
требует ровно одну из двух — иначе можно записать лог, который никуда не
ведёт, и метрики потом не с чем сопоставить.

Revision ID: 0015_social_accounts
Revises: 0014_vc_platform
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0015_social_accounts"
down_revision: str | None = "0014_vc_platform"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "social_accounts",
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
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("external_profile_id", sa.String(128), nullable=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
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
        sa.UniqueConstraint(
            "organization_id",
            "provider",
            "external_id",
            name="uq_social_accounts_org_provider_external",
        ),
    )
    op.create_index(
        "idx_social_accounts_org", "social_accounts", ["organization_id"]
    )

    op.add_column(
        "organizations",
        sa.Column("zernio_profile_id", sa.String(128), nullable=True),
    )

    op.add_column(
        "publish_logs",
        sa.Column(
            "social_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("social_accounts.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    # target_id был NOT NULL — снимаем, иначе публикация в Instagram
    # потребовала бы выдумать несуществующий телеграм-таргет.
    op.alter_column("publish_logs", "target_id", nullable=True)
    op.create_check_constraint(
        "ck_publish_logs_one_target",
        "publish_logs",
        "(target_id IS NOT NULL) <> (social_account_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_publish_logs_one_target", "publish_logs", type_="check"
    )
    # Логи публикаций через новые площадки не имеют телеграм-таргета и
    # под старую схему не подходят — удаляем их, иначе NOT NULL не встанет.
    op.execute("DELETE FROM publish_logs WHERE target_id IS NULL")
    op.drop_column("publish_logs", "social_account_id")
    op.alter_column("publish_logs", "target_id", nullable=False)
    op.drop_column("organizations", "zernio_profile_id")
    op.drop_index("idx_social_accounts_org", table_name="social_accounts")
    op.drop_table("social_accounts")
