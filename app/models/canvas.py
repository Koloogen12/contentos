import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, uuid_pk

if TYPE_CHECKING:
    from app.models.auth import Organization
    from app.models.knowledge import NodeKnowledge, Project


class NodeType(str, enum.Enum):
    SOURCE = "source"
    EXTRACT = "extract"
    FORMAT = "format"


class NodeStatus(str, enum.Enum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class SkillRunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Canvas(Base, TimestampMixin):
    __tablename__ = "canvases"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_template: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    organization: Mapped["Organization"] = relationship(back_populates="canvases")
    project: Mapped["Project | None"] = relationship(back_populates="canvases")
    nodes: Mapped[list["Node"]] = relationship(
        back_populates="canvas",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    edges: Mapped[list["Edge"]] = relationship(
        back_populates="canvas",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Node(Base, TimestampMixin):
    __tablename__ = "nodes"
    __table_args__ = (
        CheckConstraint(
            "type IN ('source', 'extract', 'format')",
            name="ck_nodes_type",
        ),
        CheckConstraint(
            "status IN ('idle', 'running', 'done', 'error')",
            name="ck_nodes_status",
        ),
        Index("idx_nodes_canvas", "canvas_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    canvas_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("canvases.id", ondelete="CASCADE"),
        nullable=False,
    )

    type: Mapped[str] = mapped_column(String(50), nullable=False)
    position_x: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    position_y: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="idle", nullable=False)

    canvas: Mapped["Canvas"] = relationship(back_populates="nodes")
    skill_runs: Mapped[list["SkillRun"]] = relationship(
        back_populates="node",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    knowledge_links: Mapped[list["NodeKnowledge"]] = relationship(
        back_populates="node",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Edge(Base, TimestampMixin):
    __tablename__ = "edges"
    __table_args__ = (
        UniqueConstraint("source_node_id", "target_node_id", name="uq_edges_pair"),
        Index("idx_edges_canvas", "canvas_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    canvas_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("canvases.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nodes.id", ondelete="CASCADE"),
        nullable=False,
    )

    canvas: Mapped["Canvas"] = relationship(back_populates="edges")


class SkillRun(Base):
    __tablename__ = "skill_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="ck_skill_runs_status",
        ),
        Index("idx_skill_runs_node", "node_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    skill: Mapped[str] = mapped_column(String(100), nullable=False)
    input_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    node: Mapped["Node"] = relationship(back_populates="skill_runs")
