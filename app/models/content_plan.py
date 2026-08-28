import uuid
from datetime import date as Date, datetime, time as Time
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date as SaDate,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Time as SaTime,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, uuid_pk


class PlannedPost(Base, TimestampMixin):
    """A scheduled / ready / drafted post in the content plan.

    Optionally linked back to the canvas + format-node it came from
    (NULLed if those are deleted; the post itself survives).
    """

    __tablename__ = "planned_posts"
    __table_args__ = (
        CheckConstraint(
            "platform IN ('telegram','instagram','linkedin','twitter','article',"
            "'carousel','reels','hooks','review','vc','stories')",
            name="ck_planned_posts_platform",
        ),
        CheckConstraint(
            "status IN ('draft','ready','scheduled','published','skipped','missed')",
            name="ck_planned_posts_status",
        ),
        CheckConstraint(
            "pillar IS NULL OR pillar IN ('R1','R2','R3','R4')",
            name="ck_planned_posts_pillar",
        ),
        Index("idx_planned_posts_org", "organization_id"),
        Index("idx_planned_posts_date", "scheduled_date"),
        Index("idx_planned_posts_status", "status"),
        Index("idx_planned_posts_platform", "platform"),
        Index("idx_planned_posts_pillar", "pillar"),
        Index("idx_planned_posts_launch", "launch_id"),
        CheckConstraint(
            "draft_state IS NULL OR draft_state IN ('writing','ready')",
            name="ck_planned_posts_draft_state",
        ),
        CheckConstraint(
            "reaction IS NULL OR reaction BETWEEN 1 AND 3",
            name="ck_planned_posts_reaction",
        ),
        CheckConstraint(
            "line_role IS NULL OR line_role IN ('announce','close')",
            name="ck_planned_posts_line_role",
        ),
        Index(
            "idx_planned_posts_launch_date",
            "launch_id",
            "scheduled_date",
            postgresql_where=text("launch_id IS NOT NULL"),
        ),
        CheckConstraint(
            "markup_origin IS NULL OR markup_origin IN ('rule','llm','human')",
            name="ck_planned_posts_markup_origin",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Source links (all nullable — manual posts have none)
    canvas_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("canvases.id", ondelete="SET NULL"),
        nullable=True,
    )
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nodes.id", ondelete="SET NULL"),
        nullable=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Content
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    hook: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cta: Mapped[str] = mapped_column(Text, nullable=False, default="")
    full_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    talking_point_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Scheduling
    scheduled_date: Mapped[Date | None] = mapped_column(SaDate, nullable=True)
    scheduled_time: Mapped[Time | None] = mapped_column(SaTime, nullable=True)

    # Status + classification
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="draft")
    pillar: Mapped[str | None] = mapped_column(String(10), nullable=True)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Publishing
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Manual metrics (views, saves, reposts, comments, clicks, ctr)
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )

    # ------------------------------------------------------------------
    # Слот прогрева
    # ------------------------------------------------------------------
    #
    # Всё, что ниже, заполнено только у единиц запуска. У обычного поста
    # эти поля пусты, и по `launch_id IS NULL` регулярный контент-план
    # отделяется от прогрева одним условием — смешивать их в одном экране
    # нельзя, иначе через месяц не понять, где ведёшь блог, а где готовишь
    # запуск.
    launch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("launches.id", ondelete="SET NULL"),
        nullable=True,
    )
    story_line_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("launch_story_lines.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: Идея банка, из которой снимается эта единица.
    knowledge_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_items.id", ondelete="SET NULL"),
        nullable=True,
    )

    launch_stage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Рубрика конструктора: что этот день должен сказать.
    meaning: Mapped[str | None] = mapped_column(String(50), nullable=True)
    trigger_key: Mapped[str | None] = mapped_column(String(50), nullable=True)
    #: Смыслы-галочки, которые закрывает эта единица.
    checkpoints: Mapped[list[str]] = mapped_column(
        ARRAY(String), default=list, nullable=False
    )
    #: Кто проставил разметку: rule / llm / human. Проверки засчитывают
    #: покрытие только по подтверждённому человеком — иначе продукт начнёт
    #: уверенно врать, что всё закрыто.
    markup_origin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: Есть ли под смыслом событие, кейс или цифра. Слова — самый
    #: неубедительный вид доказательства.
    has_proof: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Последний день окна продаж живёт по своим правилам.
    is_last_day: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Пиковый день: сильные дни ставятся подряд, а не размазываются.
    is_peak: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Правки, сделанные руками, переживают пересчёт при переносе даты.
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Оптимистичная блокировка: эксперт и продюсер правят один план, и без
    #: версии чужая работа исчезает молча.
    #: Рубрика конструктора — одна из двенадцати.
    #:
    #: До миграции 0018 рубрика лежала в колонке `meaning`, а смыслы
    #: покупателя в массиве `checkpoints`. Читалось это ровно наоборот тому,
    #: что написано в методологии, поэтому имена разведены: двенадцать
    #: рубрик — `rubric`, сорок смыслов — `meaning`.
    rubric: Mapped[str | None] = mapped_column(String(50), nullable=True)

    #: Состояние текста. `writing` — задание ушло на канвас, `ready` — текст
    #: вернулся. Нужно, чтобы «без текста» было фильтром, а не догадкой.
    draft_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    chars: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Как зашло: 1 не зашло · 2 норм · 3 зашло. На этих отметках стоит весь
    #: разбор, поэтому неотмеченные посты в статистику не идут.
    reaction: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    #: Роль в сюжетной линии: анонс или раскрытие.
    line_role: Mapped[str | None] = mapped_column(String(20), nullable=True)

    #: Почему слот пуст — дословно, как посчитал подбор.
    #:
    #: Хранится, а не собирается на лету: причина зависит от состояния банка
    #: в момент разворота, и через неделю пересчёт даст другую формулировку,
    #: а человек будет смотреть на слот и не понимать, что изменилось.
    empty_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
