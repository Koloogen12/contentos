"""Что считать действием пользователя и как о нём рассказать.

Задача звучала как «все успешные и неуспешные действия». Буквально все
запросы слать нельзя: открытая вкладка плана делает десятки GET в минуту, и
через час бот превращается в шум, который перестают читать. Поэтому действием
считается одно из двух:

* **изменение состояния** — POST, PATCH, PUT, DELETE. Человек что-то создал,
  поправил или удалил;
* **любая неудача** — ответ 4xx или 5xx, включая неудачные GET. Отказ это тоже
  результат, и о нём надо знать раньше, чем о нём напишет пользователь.

Успешные чтения не отправляются: они ничего не говорят о том, что происходит.

Список исключений ниже — не оптимизация, а защита от самозашумления: health
проверяется мониторингом каждые несколько секунд, а обновление токена
происходит у каждой вкладки раз в четверть часа.
"""
from __future__ import annotations

import re
from typing import Final

from app.services import alerts

#: Пути, которые не являются действием ни при каком исходе.
MUTED: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(p) for p in (
        r"^/health$",
        r"^/docs", r"^/redoc", r"^/openapi\.json$",
        r"^/api/v1/auth/refresh$",
        r"^/favicon\.ico$",
    )
)

#: Человеческие названия для того, что происходит чаще всего. Ключ — метод и
#: шаблон пути; чем конкретнее правило, тем выше оно в списке.
NAMES: Final[tuple[tuple[str, re.Pattern[str], str], ...]] = (
    ("POST",   re.compile(r"^/api/v1/auth/register$"),            "Регистрация"),
    ("POST",   re.compile(r"^/api/v1/auth/login$"),               "Вход"),
    ("GET",    re.compile(r"^/api/v1/auth/yandex/callback$"),     "Вход через Яндекс"),
    ("POST",   re.compile(r"^/api/v1/auth/verify"),               "Подтверждение почты"),
    ("POST",   re.compile(r"^/api/v1/launches/[^/]+/plan$"),      "Развернул план запуска"),
    ("POST",   re.compile(r"^/api/v1/launches/[^/]+/slots$"),     "Добавил слот"),
    ("POST",   re.compile(r"^/api/v1/launches$"),                 "Создал запуск"),
    ("POST",   re.compile(r"^/api/v1/nodes/[^/]+/run$"),          "Запустил ноду"),
    ("POST",   re.compile(r"^/api/v1/canvases$"),                 "Создал канвас"),
    ("POST",   re.compile(r"^/api/v1/knowledge"),                 "Добавил идею"),
    ("POST",   re.compile(r"^/api/v1/content-plan/posts/[^/]+/publish$"), "Опубликовал пост"),
    ("POST",   re.compile(r"^/api/v1/payments"),                  "Оплата"),
    ("POST",   re.compile(r"^/api/v1/skills/[^/]+/run$"),         "Запустил скилл"),
)

STATE_CHANGING: Final[frozenset[str]] = frozenset({"POST", "PATCH", "PUT", "DELETE"})


def muted(path: str) -> bool:
    return any(p.search(path) for p in MUTED)


def should_report(method: str, path: str, status: int) -> bool:
    if muted(path):
        return False
    if status >= 400:
        return True
    return method.upper() in STATE_CHANGING


def title(method: str, path: str) -> str:
    """Человеческое название действия, иначе — метод и путь."""
    for m, pattern, name in NAMES:
        if m == method.upper() and pattern.search(path):
            return name
    return f"{method.upper()} {path}"


def format_event(
    *,
    method: str,
    path: str,
    status: int,
    actor: str | None,
    duration_ms: int,
    detail: str | None = None,
) -> str:
    """Собрать сообщение так, чтобы его можно было понять с телефона."""
    ok = status < 400
    mark = "✅" if ok else ("⚠️" if status < 500 else "🔴")
    lines = [f"{mark} <b>{alerts.esc(title(method, path))}</b>"]

    who = actor or "гость"
    lines.append(f"кто: {alerts.esc(who)}")

    if not ok:
        lines.append(f"ответ: <code>{status}</code>")
        if detail:
            lines.append(f"причина: {alerts.esc(detail[:200])}")
        # При неуспехе точный путь важнее названия: по нему чинят.
        lines.append(f"<code>{alerts.esc(method.upper())} {alerts.esc(path)}</code>")

    lines.append(f"<i>{duration_ms} мс</i>")
    return "\n".join(lines)
