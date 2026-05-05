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

logger = logging.getLogger(__name__)


def _resolve_token(target: TelegramTarget) -> str:
    if target.bot_token_encrypted:
        # TODO(v2): decrypt symmetrically. For MVP we assume plaintext or empty.
        return target.bot_token_encrypted
    if settings.TELEGRAM_BOT_TOKEN:
        return settings.TELEGRAM_BOT_TOKEN
    raise RuntimeError("Telegram bot token is not configured")


async def send_message(target: TelegramTarget, text: str) -> dict[str, Any]:
    token = _resolve_token(target)
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None))
    try:
        msg = await bot.send_message(chat_id=target.chat_id, text=text, disable_web_page_preview=False)
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
