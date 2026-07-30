"""Учёт расхода токенов в skill_runs

Провайдер возвращает счётчики в каждом ответе, а мы их не сохраняли —
и не могли ответить, какой скилл съедает бюджет и помогла ли правка
промпта. Колонки nullable: у прошлых запусков этих данных нет и не
появится, а нули врали бы, что запуск был бесплатным.

Revision ID: 0016_skill_run_usage
Revises: 0015_social_accounts
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0016_skill_run_usage"
down_revision: str | None = "0015_social_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skill_runs", sa.Column("input_tokens", sa.Integer(), nullable=True))
    op.add_column("skill_runs", sa.Column("output_tokens", sa.Integer(), nullable=True))
    op.add_column(
        "skill_runs", sa.Column("cached_input_tokens", sa.Integer(), nullable=True)
    )
    op.add_column("skill_runs", sa.Column("model", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("skill_runs", "model")
    op.drop_column("skill_runs", "cached_input_tokens")
    op.drop_column("skill_runs", "output_tokens")
    op.drop_column("skill_runs", "input_tokens")
