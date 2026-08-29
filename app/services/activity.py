"""Что считать действием пользователя и как его назвать.

Задача звучала как «все успешные и неуспешные действия». Буквально все запросы
слать нельзя: открытая вкладка плана делает десятки GET в минуту. Поэтому
действие — это одно из двух:

* **изменение состояния** — POST, PATCH, PUT, DELETE: человек что-то создал,
  поправил, удалил или оплатил;
* **любая неудача** — ответ 4xx или 5xx, включая неудачные GET. Отказ это тоже
  результат, и знать о нём надо раньше, чем о нём напишет пользователь.

Успешные чтения молчат.

Названия событий здесь человеческие, а не маршрутные: «Создал запуск», а не
`POST /api/v1/launches`. Точный путь показывается только при неудаче — когда он
нужен, чтобы чинить.
"""
from __future__ import annotations

import re
from typing import Final

from app.services.alerts import Event

#: Пути, которые не являются действием ни при каком исходе. Не оптимизация, а
#: защита от самозашумления: health дёргает мониторинг каждые несколько секунд,
#: refresh — каждая открытая вкладка раз в четверть часа.
MUTED: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(p) for p in (
        r"^/health$",
        r"^/docs", r"^/redoc", r"^/openapi\.json$",
        r"^/api/v1/auth/refresh$",
        r"^/favicon\.ico$",
    )
)

#: Метод, шаблон пути, иконка, название. Порядок важен: чем конкретнее правило,
#: тем выше оно стоит.
NAMES: Final[tuple[tuple[str, re.Pattern[str], str, str], ...]] = (
    ("POST",   re.compile(r"^/api/v1/auth/register$"),                   "🆕", "Регистрация"),
    ("POST",   re.compile(r"^/api/v1/auth/verify"),                      "📬", "Подтвердил почту"),
    ("POST",   re.compile(r"^/api/v1/auth/login$"),                      "🔑", "Вход"),
    ("GET",    re.compile(r"^/api/v1/auth/yandex/callback$"),            "🔑", "Вход через Яндекс"),
    ("POST",   re.compile(r"^/api/v1/launches/[^/]+/plan$"),             "🗓", "Развернул план запуска"),
    ("POST",   re.compile(r"^/api/v1/launches/[^/]+/slots/confirm$"),    "✅", "Подтвердил разметку этапа"),
    ("POST",   re.compile(r"^/api/v1/launches/[^/]+/slots$"),            "➕", "Добавил слот"),
    ("PATCH",  re.compile(r"^/api/v1/launches/[^/]+/evidence/"),         "📎", "Отметил фактуру"),
    ("POST",   re.compile(r"^/api/v1/launches$"),                        "🚀", "Создал запуск"),
    ("POST",   re.compile(r"^/api/v1/nodes/[^/]+/run$"),                 "✨", "AI-запрос"),
    ("POST",   re.compile(r"^/api/v1/skills/[^/]+/run$"),                "✨", "Запустил скилл"),
    ("POST",   re.compile(r"^/api/v1/canvases$"),                        "🎨", "Создал канвас"),
    ("POST",   re.compile(r"^/api/v1/knowledge"),                        "💡", "Добавил идею"),
    ("POST",   re.compile(r"^/api/v1/content-plan/posts/[^/]+/publish$"),"📤", "Опубликовал пост"),
    ("POST",   re.compile(r"^/api/v1/payments"),                         "💳", "Оплата"),
    ("POST",   re.compile(r"^/api/v1/connections"),                      "🔌", "Подключил канал"),
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


def _named(method: str, path: str) -> tuple[str, str] | None:
    for m, pattern, icon, name in NAMES:
        if m == method.upper() and pattern.search(path):
            return icon, name
    return None


def to_event(
    *, method: str, path: str, status: int, actor: str | None, duration_ms: int
) -> Event:
    """Собрать событие для отправки."""
    known = _named(method, path)
    ok = status < 400

    if ok:
        icon, title = known or ("•", f"{method.upper()} {path}")
        details: dict[str, object] = {}
        # Долгий успешный запрос — тоже новость: генерации и разворот плана
        # первыми упираются в потолок.
        if duration_ms >= 3000:
            details["сек"] = round(duration_ms / 1000, 1)
        return Event(icon=icon, title=title, actor=actor, details=details or None)

    icon = "⚠️" if status < 500 else "🔴"
    title = (known[1] + " — не удалось") if known else "Ошибка"
    return Event(
        icon=icon,
        title=title,
        actor=actor,
        # При неудаче путь важнее названия: по нему чинят.
        details={"код": status, "путь": f"{method.upper()} {path}"},
    )
