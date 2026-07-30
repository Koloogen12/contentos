"""publish_log.metrics JSONB + telegram_target.public_handle

Revision ID: 0007_telegram_metrics
Revises: 0006_project_brand_visual
Create Date: 2026-05-15 14:00:00.000000

Telegram-channel metrics auto-pull (Sprint 2 Track B).

Two changes:

1. `publish_logs.metrics` — JSONB doc with the latest fetched stats per
   published post. Shape:
       {
         "views":       12345,
         "forwards":    42,
         "reactions":   {"❤️": 12, "🔥": 5},
         "fetched_at":  "2026-05-15T14:23:00Z",
         "source":      "tme_web_view"
       }
   Updated by the `pull_telegram_metrics_one` Arq task on a 6-hour cron.

2. `telegram_targets.public_handle` — optional explicit handle for the
   metrics scraper. Most users will configure `chat_id` as `@kochnefff`,
   in which case the scraper derives the handle from chat_id and this
   column stays NULL. Power users with numeric supergroup IDs
   (`-1001234567890`) can fill `public_handle` separately so we can still
   fetch their channel's metrics from the public web view. NULL handle =
   "private channel, skip metrics" — the task logs and moves on.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_telegram_metrics"
down_revision: str | None = "0006_project_brand_visual"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "publish_logs",
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "telegram_targets",
        sa.Column("public_handle", sa.String(64), nullable=True),
    )
    # Partial index so the metrics cron can cheaply find rows with views
    # set, sorted by views desc — used by the "top posts" analytics card.
    op.create_index(
        "idx_publish_logs_metrics_views",
        "publish_logs",
        [sa.text("((metrics->>'views')::int) DESC")],
        postgresql_where=sa.text("metrics IS NOT NULL AND status = 'sent'"),
    )


def downgrade() -> None:
    op.drop_index("idx_publish_logs_metrics_views", table_name="publish_logs")
    op.drop_column("telegram_targets", "public_handle")
    op.drop_column("publish_logs", "metrics")
