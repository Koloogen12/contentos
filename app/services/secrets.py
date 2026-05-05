"""Symmetric encryption for stored secrets (Fernet / AES-128-CBC + HMAC-SHA256).

We tag encrypted values with a prefix so legacy plaintext rows keep working
during rollout. New writes always go through encrypt(); reads through
decrypt() handle both forms.

Key resolution:
    1. settings.SECRETS_ENCRYPTION_KEY (must be a urlsafe-base64 32-byte
       Fernet key). Generate with:
           python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    2. Otherwise derive from JWT_SECRET via SHA-256 — fine for dev,
       NOT for prod. Logs a warning at first use.
"""
from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = logging.getLogger(__name__)

PREFIX = "enc::"

_fernet: Fernet | None = None
_warned_dev_key = False


def _resolve_key() -> bytes:
    global _warned_dev_key
    raw = (settings.SECRETS_ENCRYPTION_KEY or "").strip()
    if raw:
        return raw.encode()
    if not _warned_dev_key:
        logger.warning(
            "SECRETS_ENCRYPTION_KEY is empty; deriving from JWT_SECRET. "
            "Set SECRETS_ENCRYPTION_KEY before production deploy."
        )
        _warned_dev_key = True
    digest = hashlib.sha256(settings.JWT_SECRET.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def _cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_resolve_key())
    return _fernet


def encrypt(plaintext: str | None) -> str | None:
    """Encrypt a secret. Returns the prefixed ciphertext, or None for empty input."""
    if plaintext is None or plaintext == "":
        return None
    if plaintext.startswith(PREFIX):
        return plaintext  # already encrypted
    token = _cipher().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{PREFIX}{token}"


def decrypt(stored: str | None) -> str | None:
    """Decrypt a secret. Returns plaintext, or None for empty input.

    If the stored value lacks the prefix, it's treated as a legacy plaintext
    secret and returned as-is. This makes rollout zero-downtime.
    """
    if stored is None or stored == "":
        return None
    if not stored.startswith(PREFIX):
        return stored  # legacy plaintext
    payload = stored[len(PREFIX):].encode("ascii")
    try:
        return _cipher().decrypt(payload).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Failed to decrypt stored secret — wrong key?") from exc
