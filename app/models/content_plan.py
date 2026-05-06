import uuid
from datetime import date as Date, datetime, time as Time
from typing import Any

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Date as SaDate,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Time as SaTime,
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
            "platform IN ('telegram','instagram','linkedin','twitter','article','carousel','reels','hooks')",
            name="ck_planned_posts_platform",
        ),
        CheckConstraint(
            "status IN ('draft','ready','scheduled','published','skipped')",
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
