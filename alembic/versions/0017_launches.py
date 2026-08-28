"""Конструктор прогревов: запуски, сюжетные линии, разметка слотов и идей

Новая таблица здесь ровно одна — сам запуск. Слот календаря это строка
`planned_posts` с проставленным `launch_id`, идея — `knowledge_items` с
разметкой под прогрев. Разводить банк и календарь по разным таблицам мы
не стали намеренно: тогда карточка идеи разрезается пополам и,
провалившись в день календаря, исходную заметку уже не видно.

Все новые колонки nullable или с дефолтом: на существующих строках
регулярного контента они остаются пустыми, и по `launch_id IS NULL`
обычный контент-план отделяется от прогрева одним условием.

Revision ID: 0017_launches
Revises: 0016_skill_run_usage
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0017_launches"
down_revision: str | None = "0016_skill_run_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "launches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("product_name", sa.String(255), nullable=True),
        sa.Column("sales_open", sa.Date(), nullable=False),
        sa.Column("sales_close", sa.Date(), nullable=True),
        sa.Column("key_event_date", sa.Date(), nullable=True),
        sa.Column("key_event_type", sa.String(50), nullable=True),
        sa.Column("launch_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("intensity", sa.String(20), nullable=False, server_default="normal"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Europe/Moscow"),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("readiness", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("durations", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("waitlist_goal", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "status IN ('draft','active','paused','done','archived')",
            name="ck_launches_status",
        ),
        sa.CheckConstraint(
            "intensity IN ('light','normal','heavy')",
            name="ck_launches_intensity",
        ),
        sa.CheckConstraint(
            "sales_close IS NULL OR sales_close >= sales_open",
            name="ck_launches_sales_window",
        ),
        sa.CheckConstraint(
            "key_event_date IS NULL OR key_event_date <= sales_open",
            name="ck_launches_key_event_before_sales",
        ),
    )
    op.create_index("idx_launches_org", "launches", ["organization_id"])
    op.create_index("idx_launches_status", "launches", ["status"])
    op.create_index("idx_launches_sales_open", "launches", ["sales_open"])

    op.create_table(
        "launch_story_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("launch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("payoff", sa.Text(), nullable=True),
        sa.Column("announced_on", sa.Date(), nullable=True),
        sa.Column("closes_on", sa.Date(), nullable=True),
        sa.Column("is_closed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["launch_id"], ["launches.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_story_lines_launch", "launch_story_lines", ["launch_id"])

    # ---- planned_posts: слот прогрева --------------------------------------
    op.add_column("planned_posts", sa.Column("launch_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("planned_posts", sa.Column("story_line_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("planned_posts", sa.Column("knowledge_item_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("planned_posts", sa.Column("launch_stage", sa.Integer(), nullable=True))
    op.add_column("planned_posts", sa.Column("meaning", sa.String(50), nullable=True))
    op.add_column("planned_posts", sa.Column("trigger_key", sa.String(50), nullable=True))
    op.add_column("planned_posts", sa.Column(
        "checkpoints", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"))
    op.add_column("planned_posts", sa.Column("markup_origin", sa.String(20), nullable=True))
    op.add_column("planned_posts", sa.Column(
        "has_proof", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("planned_posts", sa.Column(
        "is_last_day", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("planned_posts", sa.Column(
        "is_peak", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("planned_posts", sa.Column(
        "is_pinned", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("planned_posts", sa.Column(
        "version", sa.Integer(), nullable=False, server_default="1"))

    op.create_foreign_key(
        "fk_planned_posts_launch", "planned_posts", "launches",
        ["launch_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_planned_posts_story_line", "planned_posts", "launch_story_lines",
        ["story_line_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_planned_posts_knowledge_item", "planned_posts", "knowledge_items",
        ["knowledge_item_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("idx_planned_posts_launch", "planned_posts", ["launch_id"])
    op.create_check_constraint(
        "ck_planned_posts_markup_origin", "planned_posts",
        "markup_origin IS NULL OR markup_origin IN ('rule','llm','human')",
    )

    # Сторис — отдельный формат, а не «инстаграм вообще»: у него своя
    # частота выхода и свои правила внутри прогрева.
    op.drop_constraint("ck_planned_posts_platform", "planned_posts", type_="check")
    # ВАЖНО: список площадок берётся из фактического состояния базы, а не
    # из модели — модель отставала, и наивная пересборка ограничения
    # выкинула бы 'review' и 'vc', добавленные миграциями 0013 и 0014.
    op.create_check_constraint(
        "ck_planned_posts_platform", "planned_posts",
        "platform IN ('telegram','instagram','linkedin','twitter','article',"
        "'carousel','reels','hooks','review','vc','stories')",
    )

    # ---- knowledge_items: разметка идеи ------------------------------------
    op.add_column("knowledge_items", sa.Column("launch_meaning", sa.String(50), nullable=True))
    op.add_column("knowledge_items", sa.Column(
        "launch_checkpoints", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"))
    op.add_column("knowledge_items", sa.Column(
        "launch_triggers", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"))
    op.add_column("knowledge_items", sa.Column("markup_origin", sa.String(20), nullable=True))
    op.add_column("knowledge_items", sa.Column(
        "markup_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("knowledge_items", sa.Column("content_format", sa.String(20), nullable=True))
    op.create_index("idx_knowledge_launch_meaning", "knowledge_items", ["launch_meaning"])


def downgrade() -> None:
    op.drop_index("idx_knowledge_launch_meaning", table_name="knowledge_items")
    for col in ("content_format", "markup_verified_at", "markup_origin",
                "launch_triggers", "launch_checkpoints", "launch_meaning"):
        op.drop_column("knowledge_items", col)

    op.drop_constraint("ck_planned_posts_platform", "planned_posts", type_="check")
    op.create_check_constraint(
        "ck_planned_posts_platform", "planned_posts",
        "platform IN ('telegram','instagram','linkedin','twitter','article',"
        "'carousel','reels','hooks','review','vc')",
    )
    op.drop_constraint("ck_planned_posts_markup_origin", "planned_posts", type_="check")
    op.drop_index("idx_planned_posts_launch", table_name="planned_posts")
    op.drop_constraint("fk_planned_posts_knowledge_item", "planned_posts", type_="foreignkey")
    op.drop_constraint("fk_planned_posts_story_line", "planned_posts", type_="foreignkey")
    op.drop_constraint("fk_planned_posts_launch", "planned_posts", type_="foreignkey")
    for col in ("version", "is_pinned", "is_peak", "is_last_day", "has_proof",
                "markup_origin", "checkpoints", "trigger_key", "meaning",
                "launch_stage", "knowledge_item_id", "story_line_id", "launch_id"):
        op.drop_column("planned_posts", col)

    op.drop_index("idx_story_lines_launch", table_name="launch_story_lines")
    op.drop_table("launch_story_lines")
    for idx in ("idx_launches_sales_open", "idx_launches_status", "idx_launches_org"):
        op.drop_index(idx, table_name="launches")
    op.drop_table("launches")
