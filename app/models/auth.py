import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, uuid_pk

if TYPE_CHECKING:
    from app.models.canvas import Canvas
    from app.models.knowledge import Project


class Organization(Base, TimestampMixin):
    """A tenant. One user = one personal org on signup; teams later.

    `kind` distinguishes anonymous trial orgs (auto-created when a
    visitor hits /try without auth) from regular orgs (created by
    /auth/register). Trial orgs have hard usage counters
    (`trial_*_used`) gated in `app/services/trial.py`; they auto-delete
    via `cleanup_expired_trials` cron 24h after the trial window ends.

    On conversion (POST /auth/convert-trial) we flip `kind='regular'`
    and clear `trial_expires_at` — the org is now permanent and the
    user keeps everything they built during the trial.
    """

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    # 'regular' | 'trial'
    kind: Mapped[str] = mapped_column(String(20), default="regular", nullable=False)
    trial_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trial_ai_runs_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    trial_renders_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    trial_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Профиль-арендатор на стороне шлюза публикации (Zernio). Заводится
    # при первом подключении Instagram/Threads/X и дальше не меняется:
    # именно он изолирует аккаунты одной организации от других.
    zernio_profile_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )

    users: Mapped[list["User"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    canvases: Mapped[list["Canvas"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    projects: Mapped[list["Project"]] = relationship(back_populates="organization", cascade="all, delete-orphan")


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # Nullable since the Yandex sign-in flow creates users without a password.
    # Never compare against this without checking it for None first —
    # `verify_password` on a NULL hash must not be allowed to pass.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    is_superuser: Mapped[bool] = mapped_column(default=False, nullable=False)

    # 'password' | 'yandex' — how the account was created. Yandex accounts
    # arrive with an address Yandex has already verified, so they skip the
    # confirmation code entirely.
    auth_provider: Mapped[str] = mapped_column(String(20), default="password", nullable=False)
    yandex_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # NULL = address not confirmed yet. Login is refused until it is set,
    # otherwise anyone could occupy someone else's address and receive the
    # workspace invitations and publish-failure notices meant for them.
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    organization: Mapped["Organization"] = relationship(back_populates="users")


class EmailVerificationCode(Base, TimestampMixin):
    """A pending sign-up confirmation code.

    Only the hash is stored: a leaked database row must not be enough to
    confirm somebody else's address. One live row per user — issuing a new
    code deletes the previous one, so an old code cannot be replayed.
    """

    __tablename__ = "email_verification_codes"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        unique=True,
    )
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Throttles "send it again" so the address cannot be used as a mail bomb.
    last_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
