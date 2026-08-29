"""Sign-up confirmation codes: issue, re-send, check.

Threat model this guards against:
  * guessing — 6 digits is only a million options, so attempts are capped and
    the row is destroyed once the cap is hit; the user has to request a new code;
  * replay — issuing a code deletes the previous one (one row per user);
  * mail bombing — re-sends are throttled per user;
  * database leak — only the hash is stored;
  * account enumeration — the caller must not reveal whether an address exists,
    so this module raises the same error for "no such user" and "no code".
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.auth import EmailVerificationCode, User
from app.services.auth import hash_password, verify_password
from app.services.mailer import send_code

CODE_LENGTH = 6


class VerificationError(Exception):
    """Something is wrong with the code or its state.

    `code` is a stable machine-readable reason for the frontend;
    `message` is shown to the user as-is.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def generate_code() -> str:
    """Zero-padded 6 digits from a CSPRNG.

    `secrets.randbelow` rather than `random`: the latter is seeded predictably
    and its output would be reconstructable from a couple of observed codes.
    """
    return f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


async def issue_code(db: AsyncSession, user: User, *, force: bool = False) -> tuple[str, bool]:
    """Create (or re-create) the pending code for `user` and mail it.

    Returns `(code, delivered)` — `delivered` is False when SMTP is not
    configured, in which case the code sits in the server log. The plaintext
    is returned so callers can log it in that mode; never put it in a response.

    `force=False` enforces the re-send cooldown.
    """
    existing = await db.scalar(
        select(EmailVerificationCode).where(EmailVerificationCode.user_id == user.id)
    )
    if existing is not None and not force:
        cooldown = timedelta(seconds=settings.EMAIL_CODE_RESEND_COOLDOWN_SECONDS)
        waited = _now() - existing.last_sent_at
        if waited < cooldown:
            left = int((cooldown - waited).total_seconds())
            raise VerificationError(
                "cooldown",
                f"Новый код можно запросить через {left} с.",
            )

    code = generate_code()
    if existing is not None:
        await db.execute(
            delete(EmailVerificationCode).where(EmailVerificationCode.id == existing.id)
        )
        await db.flush()

    db.add(
        EmailVerificationCode(
            user_id=user.id,
            code_hash=hash_password(code),
            expires_at=_now() + timedelta(minutes=settings.EMAIL_CODE_TTL_MINUTES),
            last_sent_at=_now(),
            attempts=0,
        )
    )
    await db.flush()

    delivered = await send_code(to=user.email, code=code)
    return code, delivered


async def verify_code(db: AsyncSession, user: User, code: str) -> None:
    """Confirm `user`'s address, or raise VerificationError.

    On success the code row is removed and `email_verified_at` is stamped.
    """
    row = await db.scalar(
        select(EmailVerificationCode).where(EmailVerificationCode.user_id == user.id)
    )
    if row is None:
        raise VerificationError("no_code", "Код не запрашивался или уже использован.")

    # Every failure path below must COMMIT before raising. The request-scoped
    # session (app/database.py) commits only on success and rolls back on any
    # exception — so a plain flush() here is discarded together with the error
    # response. Measured on prod before this fix: the attempt counter never
    # advanced past 1, the cap never tripped, and a six-digit code could be
    # brute-forced without limit.
    async def fail(code_: str, message: str) -> None:
        await db.commit()
        raise VerificationError(code_, message)

    if row.expires_at <= _now():
        await db.execute(delete(EmailVerificationCode).where(EmailVerificationCode.id == row.id))
        await fail("expired", "Код истёк. Запроси новый.")

    if row.attempts >= settings.EMAIL_CODE_MAX_ATTEMPTS:
        await db.execute(delete(EmailVerificationCode).where(EmailVerificationCode.id == row.id))
        await fail("too_many_attempts", "Слишком много попыток. Запроси новый код.")

    if not verify_password(code, row.code_hash):
        row.attempts += 1
        left = settings.EMAIL_CODE_MAX_ATTEMPTS - row.attempts
        await fail(
            "invalid",
            "Неверный код." + (f" Осталось попыток: {left}." if left > 0 else ""),
        )

    user.email_verified_at = _now()
    await db.execute(delete(EmailVerificationCode).where(EmailVerificationCode.id == row.id))
    await db.flush()
