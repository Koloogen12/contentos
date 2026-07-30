"""LinkedIn OAuth-connected identities."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, uuid_pk


class LinkedInAccount(Base, TimestampMixin):
    """One LinkedIn user authenticated via OAuth for one organization.

    Tokens are encrypted via `services/secrets.encrypt` before write — the
    `*_encrypted` suffix on the columns is a contract: never read them
    directly, always go through `services.linkedin_oauth.get_access_token`
    (which handles refresh too).

    See migration 0008_linkedin_accounts for the column schema.
    """

    __tablename__ = "linkedin_accounts"
    __table_args__ = (
        Index("idx_linkedin_accounts_org", "organization_id"),
        UniqueConstraint(
            "organization_id", "sub", name="uq_linkedin_accounts_org_sub"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )

    # LinkedIn OIDC subject — stable per LinkedIn user.
    sub: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    refresh_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(String), default=list, nullable=False
    )
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
