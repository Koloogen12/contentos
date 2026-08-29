"""Уведомления о действиях пользователей в Telegram.

Отдельно от `telegram_bot.py`: тот публикует контент в каналы клиентов, этот
пишет владельцу продукта, что происходит на сервере. Разные адресаты, разные
токены, разная цена ошибки — смешивать нельзя.

Два правила, которые здесь важнее удобства.

**Отправка никогда не ломает запрос.** Пользователь не должен получить пятисотку
из-за того, что Telegram недоступен или чат удалён. Любое исключение
проглатывается и уходит в лог.

**Отправка не задерживает ответ.** Сообщение уходит фоновой задачей: HTTP к
Telegram занимает сотни миллисекунд, и ставить их в критический путь каждого
действия — значит замедлить продукт ради наблюдения за ним.
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"

#: Фоновые задачи держим за ссылки: без этого сборщик мусора может убить
#: задачу до того, как она успеет отправиться.
_pending: set[asyncio.Task[Any]] = set()


def configured() -> bool:
    return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_ALERT_CHAT_ID)


async def _post(text: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                API.format(token=settings.TELEGRAM_BOT_TOKEN),
                json={
                    "chat_id": settings.TELEGRAM_ALERT_CHAT_ID,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            if r.status_code != 200:
                log.warning("alerts: telegram ответил %s — %s", r.status_code, r.text[:200])
    except Exception as exc:  # noqa: BLE001 — уведомление не имеет права падать
        log.warning("alerts: не отправлено — %s: %s", type(exc).__name__, exc)


def send(text: str) -> None:
    """Поставить сообщение в отправку и сразу вернуть управление."""
    if not configured():
        return
    try:
        task = asyncio.create_task(_post(text))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    except RuntimeError:
        # Нет работающего цикла событий — например, вызов из синхронного
        # контекста. Молча пропускаем: уведомление не стоит того, чтобы
        # ради него поднимать цикл.
        log.debug("alerts: нет цикла событий, сообщение пропущено")


def esc(value: Any) -> str:
    """Экранировать значение для HTML-разметки Telegram."""
    return html.escape(str(value), quote=False)
