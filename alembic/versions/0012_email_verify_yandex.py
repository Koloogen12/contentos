"""users: email confirmation + Yandex sign-in

Revision ID: 0012_email_verify_yandex
Revises: 0011_llm_node
Create Date: 2026-07-27 12:00:00.000000

Sign-up now requires confirming the address with a code, and an account can
also be created by signing in with Yandex.

Three things change on `users`:
  * `email_verified_at` — NULL means unconfirmed; login is refused.
  * `auth_provider` / `yandex_user_id` — where the account came from.
  * `password_hash` becomes nullable, because Yandex accounts have no password.

Existing accounts are backfilled as confirmed. They were created before the
requirement existed and their owners are already using the product — forcing
them through a confirmation they never agreed to would lock them out of their
own workspaces (and prod has live users).

New table `email_verification_codes` keeps one pending code per user, stored
as a hash so a leaked row cannot be used to confirm someone else's address.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_email_verify_yandex"
down_revision: str | None = "0011_llm_node"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "auth_provider",
            sa.String(length=20),
            nullable=False,
            server_default="password",
        ),
    )
    op.add_column(
        "users",
        sa.Column("yandex_user_id", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_users_yandex_user_id", "users", ["yandex_user_id"])
    op.alter_column("users", "password_hash", existing_type=sa.String(255), nullable=True)

    # Everyone who signed up before this migration keeps their access.
    op.execute("UPDATE users SET email_verified_at = created_at WHERE email_verified_at IS NULL")

    op.create_table(
        "email_verification_codes",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("code_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_email_verification_codes_user_id", "email_verification_codes", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_email_verification_codes_user_id", table_name="email_verification_codes")
    op.drop_table("email_verification_codes")
    # Rows with a NULL password_hash are Yandex-only accounts: they cannot be
    # represented once the column is NOT NULL again, and giving them a random
    # hash would silently create an account nobody can sign into. Fail loudly.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM users WHERE password_hash IS NULL) THEN
                RAISE EXCEPTION
                    'Cannot downgrade: % Yandex-only account(s) have no password.',
                    (SELECT count(*) FROM users WHERE password_hash IS NULL);
            END IF;
        END $$;
        """
    )
    op.alter_column("users", "password_hash", existing_type=sa.String(255), nullable=False)
    op.drop_index("ix_users_yandex_user_id", table_name="users")
    op.drop_column("users", "yandex_user_id")
    op.drop_column("users", "auth_provider")
    op.drop_column("users", "email_verified_at")
