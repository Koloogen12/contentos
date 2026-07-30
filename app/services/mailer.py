"""Outgoing mail via Resend.

The product sends exactly one kind of message today — the sign-up confirmation
code — so this stays deliberately small: no template engine, no queue.

Resend rather than raw SMTP: the same account already serves another service on
this host (`/etc/slotix/secrets.env`) with `neurin.tech` verified, so there is
no new mailbox, no app password and no SPF/DKIM work to do. It is also an HTTPS
call, which means no long-lived socket and no port-25/465 egress question.

When `RESEND_API_KEY` is empty the code is written to the log instead of being
mailed. That keeps local development usable without credentials:
`docker compose logs api | grep 'confirmation code'`. Silently dropping the
mail would look like the flow works when it doesn't, so the log line is loud
and `send_mail` reports back whether it actually sent.
"""

from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"


def mail_configured() -> bool:
    return bool(settings.RESEND_API_KEY and settings.MAIL_FROM)


def _sender() -> str:
    name = settings.MAIL_FROM_NAME.strip()
    return f"{name} <{settings.MAIL_FROM}>" if name else settings.MAIL_FROM


async def send_mail(*, to: str, subject: str, text: str) -> bool:
    """Send one plain-text message. Returns True if Resend accepted it.

    Never raises: a mail outage must not turn into a failed registration the
    user cannot retry. The caller decides what to tell them.
    """
    if not mail_configured():
        logger.warning(
            "Mail is not configured (RESEND_API_KEY/MAIL_FROM empty) — message to %s "
            "was NOT sent. Body follows so the flow can still be completed manually:\n%s",
            to,
            text,
        )
        return False

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                RESEND_ENDPOINT,
                headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
                json={
                    "from": _sender(),
                    "to": [to],
                    "subject": subject,
                    # Plain text only. An HTML twin would have to be kept in
                    # sync with it, and a six-digit code needs no layout.
                    "text": text,
                },
            )
    except Exception:
        logger.exception("Resend request failed for %s", to)
        return False

    if response.status_code >= 300:
        # Body is logged because Resend explains refusals precisely
        # (unverified sender domain, invalid address, rate limit).
        logger.error(
            "Resend refused mail to %s: %s %s", to, response.status_code, response.text
        )
        return False
    return True


CODE_SUBJECT = "Код подтверждения THE DRAFT"


def code_body(code: str) -> str:
    minutes = settings.EMAIL_CODE_TTL_MINUTES
    return (
        f"Код подтверждения: {code}\n\n"
        f"Введи его на странице регистрации. Код действует {minutes} минут.\n\n"
        "Если код запрашивал не ты — просто проигнорируй письмо: "
        "без подтверждения аккаунт не активируется.\n\n"
        "THE DRAFT"
    )
