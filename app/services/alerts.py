"""Уведомления о действиях пользователей в Telegram.

Отдельно от `telegram_bot.py`: тот публикует контент в каналы клиентов, этот
пишет владельцу продукта. Разные адресаты, разная цена ошибки.

**События уходят пачками, а не по одному.** Регистрация тянет за собой
подтверждение почты, вход и первые запросы — по сообщению на каждое, и через
минуту лента превращается в поток, который перестают читать. Поэтому события
копятся в окне и отправляются одним сообщением со счётчиком:

    📡 Активность (2)
    🎓 Онбординг завершён — Николай Хвилон <nkhvilon@gmail.com> (project=…)
    ✨ Первый AI-запрос — Николай Хвилон <nkhvilon@gmail.com> (module=…)

Два правила, которые важнее удобства.

**Отправка никогда не ломает запрос.** Пользователь не должен получить
пятисотку из-за того, что Telegram недоступен. Любое исключение проглатывается
и уходит в лог.

**Отправка не задерживает ответ.** Событие кладётся в буфер за микросекунды,
HTTP к Telegram живёт в фоновой задаче.
"""
from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"

#: Сколько ждём перед отправкой пачки. Достаточно коротко, чтобы уведомление
#: оставалось живым, и достаточно длинно, чтобы связанные события одного
#: человека попали в одно сообщение.
WINDOW_SECONDS = 20.0

#: Предохранитель от потока: при всплеске отправляем не дожидаясь окна.
MAX_BATCH = 20

#: Сколько событий держим в буфере максимум. Если Telegram лежит долго,
#: буфер не должен съесть память — старые события отбрасываются.
MAX_BUFFER = 200


@dataclass(frozen=True)
class Event:
    """Одно действие. `icon` и `title` — то, что видит человек."""

    icon: str
    title: str
    actor: str | None = None
    #: Подробности вида `module=generate-job-graph` — только то, что помогает
    #: понять событие без открывания админки.
    details: dict[str, Any] | None = None


_buffer: list[Event] = []
_flush_task: asyncio.Task[Any] | None = None
_pending: set[asyncio.Task[Any]] = set()
_lock = asyncio.Lock()


def configured() -> bool:
    return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_ALERT_CHAT_ID)


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def render(events: list[Event]) -> str:
    lines = [f"📡 <b>Активность ({len(events)})</b>", ""]
    for e in events:
        row = f"{e.icon} {esc(e.title)}"
        if e.actor:
            row += f" — {esc(e.actor)}"
        if e.details:
            pairs = " ".join(f"{k}={v}" for k, v in e.details.items() if v not in (None, ""))
            if pairs:
                row += f" <i>({esc(pairs)})</i>"
        lines.append(row)
    return "\n".join(lines)


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


async def flush() -> None:
    """Отправить накопленное. Безопасно вызывать когда угодно."""
    async with _lock:
        if not _buffer:
            return
        batch, _buffer[:] = list(_buffer), []
    await _post(render(batch))


async def _flush_after_window() -> None:
    try:
        await asyncio.sleep(WINDOW_SECONDS)
        await flush()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("alerts: окно отправки упало — %s", exc)


def _spawn(coro: Any) -> asyncio.Task[Any] | None:
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        # Нет работающего цикла — вызов из синхронного контекста. Ради
        # уведомления поднимать цикл не будем.
        log.debug("alerts: нет цикла событий, событие пропущено")
        return None
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


def push(event: Event) -> None:
    """Положить событие в очередь отправки."""
    global _flush_task
    if not configured():
        return

    if len(_buffer) >= MAX_BUFFER:
        # Лучше потерять самое старое, чем расти без предела.
        _buffer.pop(0)
    _buffer.append(event)

    if len(_buffer) >= MAX_BATCH:
        if _flush_task and not _flush_task.done():
            _flush_task.cancel()
        _flush_task = None
        _spawn(flush())
        return

    if _flush_task is None or _flush_task.done():
        _flush_task = _spawn(_flush_after_window())


def event(icon: str, title: str, *, actor: str | None = None, **details: Any) -> None:
    """Сообщить о доменном событии из кода приложения.

        alerts.event("✨", "Первый AI-запрос", actor=who, module="generate-job-graph")

    Для событий, которые не выводятся из HTTP-запроса: завершённый онбординг,
    первая генерация, успешная оплата.
    """
    push(Event(icon=icon, title=title, actor=actor, details=details or None))


def actor_of(user: Any) -> str | None:
    """«Имя <почта>» — так адресата видно с телефона без догадок."""
    if user is None:
        return None
    name = getattr(user, "display_name", None) or getattr(user, "name", None)
    mail = getattr(user, "email", None)
    if name and mail:
        return f"{name} <{mail}>"
    return mail or (str(getattr(user, "id", "")) or None)
