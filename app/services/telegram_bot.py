"""Thin aiogram wrapper for Telegram publishing.

Bot token resolution order:
    1. target.bot_token_encrypted (when org-level bot — V2; today not encrypted yet)
    2. settings.TELEGRAM_BOT_TOKEN (single shared bot)
"""
from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.client.default import DefaultBotProperties

from app.config import settings
from app.models.publish import TelegramTarget
from app.services import secrets

logger = logging.getLogger(__name__)


def _resolve_token(target: TelegramTarget) -> str:
    if target.bot_token_encrypted:
        decrypted = secrets.decrypt(target.bot_token_encrypted)
        if decrypted:
            return decrypted
    if settings.TELEGRAM_BOT_TOKEN:
        return settings.TELEGRAM_BOT_TOKEN
    raise RuntimeError("Telegram bot token is not configured")


async def send_message(target: TelegramTarget, text: str) -> dict[str, Any]:
    """Send a post to a Telegram channel/chat.

    The text may contain Telegram-HTML tags (`<b>`, `<i>`, `<s>`,
    `<tg-spoiler>`, `<blockquote>`, `<code>`). We send with
    `parse_mode="HTML"` so they render as formatting; bare angle brackets
    in user content must be escaped beforehand (the format-skill prompt
    handles this — see telegram_creator.SYSTEM_TEMPLATE).

    If parsing fails (rare — the skill controls the markup), we retry
    once with `parse_mode=None` so the user at least gets the raw text
    rather than a 400 "Bad Request: can't parse entities".
    """
    token = _resolve_token(target)
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    try:
        try:
            msg = await bot.send_message(
                chat_id=target.chat_id,
                text=text,
                disable_web_page_preview=False,
            )
        except TelegramAPIError as exc:
            err = str(exc).lower()
            # Telegram's HTML parser errors look like "can't parse entities:
            # ...". They're our format-skill's fault, not the user's — fall
            # back to a plain-text send so the post still goes out. We log
            # the original error so a real misformat surfaces in monitoring.
            if "can't parse entities" in err or "can’t parse entities" in err:
                logger.warning(
                    "telegram html parse failed, retrying as plain: %s", exc
                )
                msg = await bot.send_message(
                    chat_id=target.chat_id,
                    text=text,
                    disable_web_page_preview=False,
                    parse_mode=None,
                )
            else:
                raise
        return {
            "message_id": msg.message_id,
            "chat_id": msg.chat.id,
            "date": int(msg.date.timestamp()) if msg.date else None,
        }
    except TelegramAPIError as exc:
        logger.warning("telegram api error: %s", exc)
        raise
    finally:
        await bot.session.close()
