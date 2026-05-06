import enum
import uuid
from typing import TYPE_CHECKING, Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, uuid_pk

if TYPE_CHECKING:
    from app.models.auth import Organization
    from app.models.canvas import Canvas, Node


class KnowledgeItemType(str, enum.Enum):
    TEZIS = "tezis"
    REFERENCE = "reference"
    AUDIENCE = "audience"
    VOICE_RULE = "voice_rule"
    CONTENT_THEME = "content_theme"
    MANIFESTO = "manifesto"


class Project(Base, TimestampMixin):
    """Container for canvases under one logical brand/product (e.g. THE MONO)."""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    color: Mapped[str] = mapped_column(String(50), default="#6366f1", nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    organization: Mapped["Organization"] = relationship(back_populates="projects")
    canvases: Mapped[list["Canvas"]] = relationship(back_populates="project")


class BrandContext(Base, TimestampMixin):
    """Per-organization brand voice + manifesto + taboo. One row per org."""

    __tablename__ = "brand_contexts"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class KnowledgeItem(Base, TimestampMixin):
    __tablename__ = "knowledge_items"
    __table_args__ = (
        CheckConstraint(
            "type IN ('tezis','reference','audience','voice_rule','content_theme','manifesto')",
            name="ck_knowledge_items_type",
        ),
        CheckConstraint(
            "pillar IS NULL OR pillar IN ('R1','R2','R3','R4')",
            name="ck_knowledge_items_pillar",
        ),
        Index("idx_knowledge_org", "organization_id"),
        Index("idx_knowledge_type", "type"),
        Index("idx_knowledge_project", "project_id"),
        Index(
            "idx_knowledge_score",
            "viral_score",
            postgresql_using="btree",
        ),
        Index("idx_knowledge_dormant", "is_dormant", "last_used_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
    )

    type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    viral_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pillar: Mapped[str | None] = mapped_column(String(10), nullable=True)
    source_file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_dormant: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_used_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)


class NodeKnowledge(Base):
    """Many-to-many: which knowledge items are attached to a node for context injection."""

    __tablename__ = "node_knowledge"

    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nodes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    knowledge_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_items.id", ondelete="CASCADE"),
        primary_key=True,
    )
    attached_at: Mapped["DateTime"] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    node: Mapped["Node"] = relationship(back_populates="knowledge_links")


class VoiceSample(Base, TimestampMixin):
    """Sample post by the user used for few-shot voice retrieval (pgvector)."""

    __tablename__ = "voice_samples"
    __table_args__ = (
        Index("idx_voice_samples_org", "organization_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
    )

    platform: Mapped[str | None] = mapped_column(String(50), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
