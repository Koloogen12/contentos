"""Запуски: фактура, ведение слотов, привязка линий к постам.

Модуль «Запуски» на клиенте умел больше, чем контракт: переносил слоты,
отмечал выпущенное, вёл инвентаризацию сорока смыслов и связывал анонс с
конкретным постом. Всё это жило в состоянии вкладки и терялось при
перезагрузке. Миграция даёт этому место в базе.

Четыре решения, которые видно в схеме:

1. Статус запуска не хранится и здесь не появляется. `draft · warm · sales ·
   closed` — функция `unrolled_on`, `sales_open`, `sales_close` и
   `archived_at`. Хранимая копия расходится с датами на первом же переносе
   окна. Добавляется только `unrolled_on` — единственное, что не выводится.

2. Имена приведены к спеке методологии: двенадцать конструкторских рубрик —
   `rubric`, сорок смыслов покупателя — `meaning`. До этой миграции рубрика
   лежала в колонке `meaning`, а смыслы в массиве `checkpoints`, и любой,
   кто открывал таблицу впервые, читал её неверно. Данные переносятся, а не
   бросаются.

3. Фактура — отдельная таблица с составным ключом (запуск, смысл). Класть её
   в JSONB на запуске значило бы потерять возможность спросить «где у нас
   нет пруфов по третьему вопросу» одним запросом.

4. Задач на добычу как сущности нет. Задача — это ровно «фактура в состоянии
   none плюс дедлайн из плана»; хранить её значит держать вторую копию того
   же факта и ловить расхождения. Персистится только то, что не вычисляется:
   отметка «убрал из списка».
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_launch_production"
down_revision: str | None = "0017_launches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── запуск: поля рамки, которых не хватало интерфейсу ──────────────────
    op.add_column("launches", sa.Column("price", sa.String(length=100), nullable=True))
    op.add_column("launches", sa.Column("audience", sa.String(length=255), nullable=True))
    op.add_column("launches", sa.Column("collect", sa.String(length=255), nullable=True))
    op.add_column("launches", sa.Column("waitlist", sa.Integer(), nullable=True))
    op.add_column("launches", sa.Column("paid", sa.Integer(), nullable=True))
    op.add_column("launches", sa.Column("paid_goal", sa.Integer(), nullable=True))
    # День разворота плана. Отличает черновик от идущего запуска — и это
    # единственная часть статуса, которую нельзя вывести из дат.
    op.add_column("launches", sa.Column("unrolled_on", sa.Date(), nullable=True))

    # Проставляем задним числом: у запусков, где уже есть слоты, план явно
    # разворачивали. Без этого они после деплоя показались бы черновиками.
    op.execute(
        """
        UPDATE launches l
        SET unrolled_on = sub.first_day
        FROM (
            SELECT launch_id, MIN(scheduled_date) AS first_day
            FROM planned_posts
            WHERE launch_id IS NOT NULL
            GROUP BY launch_id
        ) sub
        WHERE l.id = sub.launch_id
        """
    )

    # `dense` — имя из прототипа для той же плотной каденции, что бэкенд
    # звал `heavy`. Принимаем оба, чтобы клиент не переводил туда-обратно.
    op.drop_constraint("ck_launches_intensity", "launches", type_="check")
    op.create_check_constraint(
        "ck_launches_intensity",
        "launches",
        "intensity IN ('light','normal','heavy','dense')",
    )

    # ── слот: рубрика отдельно от смысла ──────────────────────────────────
    op.add_column("planned_posts", sa.Column("rubric", sa.String(length=50), nullable=True))
    # До миграции в `meaning` лежала рубрика — переносим и освобождаем поле
    # под смысл покупателя.
    op.execute("UPDATE planned_posts SET rubric = meaning WHERE launch_id IS NOT NULL")
    op.execute(
        """
        UPDATE planned_posts
        SET meaning = checkpoints[1]
        WHERE launch_id IS NOT NULL
          AND checkpoints IS NOT NULL
          AND array_length(checkpoints, 1) >= 1
        """
    )
    op.execute(
        "UPDATE planned_posts SET meaning = NULL "
        "WHERE launch_id IS NOT NULL AND meaning = rubric"
    )

    # ── слот: ведение запуска ─────────────────────────────────────────────
    op.add_column("planned_posts", sa.Column("draft_state", sa.String(length=20), nullable=True))
    op.add_column("planned_posts", sa.Column("chars", sa.Integer(), nullable=True))
    op.add_column("planned_posts", sa.Column("reaction", sa.SmallInteger(), nullable=True))
    op.add_column("planned_posts", sa.Column("line_role", sa.String(length=20), nullable=True))
    op.add_column("planned_posts", sa.Column("empty_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_planned_posts_draft_state",
        "planned_posts",
        "draft_state IS NULL OR draft_state IN ('writing','ready')",
    )
    op.create_check_constraint(
        "ck_planned_posts_reaction",
        "planned_posts",
        "reaction IS NULL OR reaction BETWEEN 1 AND 3",
    )
    op.create_check_constraint(
        "ck_planned_posts_line_role",
        "planned_posts",
        "line_role IS NULL OR line_role IN ('announce','close')",
    )

    # `missed` — «день прошёл, пост не вышел». Отличается от `skipped`:
    # skipped это решение автора, missed — факт, и разбор считает их розно.
    op.drop_constraint("ck_planned_posts_status", "planned_posts", type_="check")
    op.create_check_constraint(
        "ck_planned_posts_status",
        "planned_posts",
        "status IN ('draft','ready','scheduled','published','skipped','missed')",
    )

    # Слоты всегда читаются диапазоном дат внутри запуска.
    op.create_index(
        "idx_planned_posts_launch_date",
        "planned_posts",
        ["launch_id", "scheduled_date"],
        postgresql_where=sa.text("launch_id IS NOT NULL"),
    )

    # ── сюжетная линия: обещание живёт постом, а не датой ─────────────────
    op.add_column(
        "launch_story_lines",
        sa.Column("announce_slot_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "launch_story_lines",
        sa.Column("close_slot_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # ON DELETE SET NULL: удаление слота не должно уносить линию — обещание
    # остаётся, просто снова становится незакрытым.
    op.create_foreign_key(
        "fk_story_lines_announce_slot", "launch_story_lines", "planned_posts",
        ["announce_slot_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_story_lines_close_slot", "launch_story_lines", "planned_posts",
        ["close_slot_id"], ["id"], ondelete="SET NULL",
    )

    # ── фактура: одна строка на (запуск, смысл) ───────────────────────────
    op.create_table(
        "launch_evidence",
        sa.Column("launch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("meaning_key", sa.String(length=50), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="none"),
        sa.Column("proof_note", sa.Text(), nullable=True),
        sa.Column("proof_url", sa.Text(), nullable=True),
        # Задача снята с доски вручную. Единственное, что здесь хранится и
        # не выводится из состояния.
        sa.Column("task_dismissed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["launch_id"], ["launches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("launch_id", "meaning_key"),
        sa.CheckConstraint(
            "state IN ('proof','claimed','none')", name="ck_launch_evidence_state"
        ),
    )
    # «Где нет пруфов» — самый частый вопрос к таблице.
    op.create_index(
        "idx_launch_evidence_open",
        "launch_evidence",
        ["launch_id", "state"],
        postgresql_where=sa.text("state <> 'proof'"),
    )


def downgrade() -> None:
    op.drop_index("idx_launch_evidence_open", table_name="launch_evidence")
    op.drop_table("launch_evidence")

    op.drop_constraint("fk_story_lines_close_slot", "launch_story_lines", type_="foreignkey")
    op.drop_constraint("fk_story_lines_announce_slot", "launch_story_lines", type_="foreignkey")
    op.drop_column("launch_story_lines", "close_slot_id")
    op.drop_column("launch_story_lines", "announce_slot_id")

    op.drop_index("idx_planned_posts_launch_date", table_name="planned_posts")

    op.drop_constraint("ck_planned_posts_status", "planned_posts", type_="check")
    op.create_check_constraint(
        "ck_planned_posts_status",
        "planned_posts",
        "status IN ('draft','ready','scheduled','published','skipped')",
    )
    op.drop_constraint("ck_planned_posts_line_role", "planned_posts", type_="check")
    op.drop_constraint("ck_planned_posts_reaction", "planned_posts", type_="check")
    op.drop_constraint("ck_planned_posts_draft_state", "planned_posts", type_="check")
    op.drop_column("planned_posts", "empty_reason")
    op.drop_column("planned_posts", "line_role")
    op.drop_column("planned_posts", "reaction")
    op.drop_column("planned_posts", "chars")
    op.drop_column("planned_posts", "draft_state")

    # Возвращаем рубрику в `meaning`, как было до миграции.
    op.execute("UPDATE planned_posts SET meaning = rubric WHERE launch_id IS NOT NULL")
    op.drop_column("planned_posts", "rubric")

    op.drop_constraint("ck_launches_intensity", "launches", type_="check")
    op.create_check_constraint(
        "ck_launches_intensity", "launches", "intensity IN ('light','normal','heavy')"
    )
    op.drop_column("launches", "unrolled_on")
    op.drop_column("launches", "paid_goal")
    op.drop_column("launches", "paid")
    op.drop_column("launches", "waitlist")
    op.drop_column("launches", "collect")
    op.drop_column("launches", "audience")
    op.drop_column("launches", "price")
