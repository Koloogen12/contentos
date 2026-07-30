"""Клиент Zernio — внешний шлюз публикации в Instagram, Threads и X.

Зачем посредник, когда для Telegram и LinkedIn мы ходим на площадки сами:
у Meta публикация в Instagram и Threads открывается только приложению,
прошедшему App Review — это недели ожидания и право на отказ. У X запись
закрыта платным тарифом с фиксированной платой. Zernio публикует через
своё уже одобренное приложение Meta и пробрасывает стоимость запросов X,
поэтому обе преграды снимаются деньгами, а не месяцами.

Где посредник не нужен, его и нет: Telegram публикуется нашим ботом
бесплатно, LinkedIn — нашим приложением, которое регистрируется за час без
ревью. Платить за них по $6 в месяц за аккаунт незачем.

Границы ответственности: этот модуль знает только HTTP-контракт Zernio.
Решение «через кого публиковать» принимает app/services/publishing.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://zernio.com/api"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Площадки, которые мы доверяем этому провайдеру. Список намеренно узкий:
# Zernio умеет 16 площадок, но платим мы за каждый подключённый аккаунт,
# поэтому туда идёт только то, что своими силами дорого или невозможно.
SUPPORTED_PLATFORMS = ("instagram", "threads", "x")


class ZernioError(RuntimeError):
    """Ошибка со стороны шлюза — с текстом, пригодным для показа."""


class ZernioNotConfigured(ZernioError):
    def __init__(self) -> None:
        super().__init__(
            "Публикация через Zernio не настроена: на сервере нет "
            "ZERNIO_API_KEY."
        )


def is_configured() -> bool:
    return bool(settings.ZERNIO_API_KEY)


def _headers() -> dict[str, str]:
    if not settings.ZERNIO_API_KEY:
        raise ZernioNotConfigured()
    return {
        "Authorization": f"Bearer {settings.ZERNIO_API_KEY}",
        "Content-Type": "application/json",
    }


async def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
) -> Any:
    url = f"{BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            resp = await client.request(
                method, url, headers=_headers(), params=params, json=json
            )
        except httpx.RequestError as exc:
            logger.warning("zernio %s %s — сеть недоступна: %s", method, path, exc)
            raise ZernioError(
                "Шлюз публикации недоступен. Попробуй ещё раз через минуту."
            ) from exc

    if resp.status_code >= 400:
        # Текст ошибки провайдера показываем как есть: он про конкретную
        # площадку («Instagram требует Business-аккаунт») и полезнее, чем
        # наша обобщённая формулировка.
        detail = ""
        try:
            body = resp.json()
            detail = body.get("error") or body.get("message") or ""
        except Exception:
            detail = resp.text[:200]
        logger.warning(
            "zernio %s %s → %s: %s", method, path, resp.status_code, detail
        )
        raise ZernioError(detail or f"Шлюз ответил {resp.status_code}")

    if not resp.content:
        return None
    return resp.json()


# ---------------------------------------------------------------------------
# Профили: один на организацию
# ---------------------------------------------------------------------------


async def create_profile(name: str) -> str:
    """Создать профиль-арендатор и вернуть его id.

    В модели Zernio профиль — граница между клиентами: подключённые
    аккаунты живут внутри него. Мы заводим по профилю на организацию,
    чтобы аккаунты одного клиента нельзя было увидеть или задеть из
    другого.
    """
    data = await _request("POST", "/v1/profiles", json={"name": name})
    profile_id = (data or {}).get("_id") or (data or {}).get("id")
    if not profile_id:
        raise ZernioError("Шлюз не вернул идентификатор профиля")
    return str(profile_id)


# ---------------------------------------------------------------------------
# Подключение аккаунтов
# ---------------------------------------------------------------------------


async def connect_url(platform: str, profile_id: str, redirect_url: str) -> str:
    """Ссылка, по которой пользователь авторизует свой аккаунт площадки."""
    if platform not in SUPPORTED_PLATFORMS:
        raise ZernioError(f"Площадка «{platform}» не подключается через шлюз")
    data = await _request(
        "GET",
        f"/v1/connect/{platform}",
        params={"profileId": profile_id, "redirect_url": redirect_url},
    )
    url = (data or {}).get("url") or (data or {}).get("connectUrl")
    if not url:
        raise ZernioError("Шлюз не вернул ссылку авторизации")
    return str(url)


async def list_accounts(profile_id: str) -> list[dict[str, Any]]:
    """Аккаунты, подключённые к профилю.

    Источник правды о подключениях — шлюз, а не наша база: пользователь
    может отозвать доступ на стороне площадки, и мы узнаем об этом только
    отсюда.
    """
    data = await _request(
        "GET", "/v1/accounts", params={"profileId": profile_id}
    )
    if isinstance(data, list):
        return data
    return (data or {}).get("accounts") or []


async def disconnect_account(account_id: str) -> None:
    await _request("DELETE", f"/v1/accounts/{account_id}")


# ---------------------------------------------------------------------------
# Публикация
# ---------------------------------------------------------------------------


async def publish(
    *,
    platform: str,
    account_id: str,
    content: str,
    media_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Опубликовать сейчас. Возвращает ответ шлюза с id поста."""
    payload: dict[str, Any] = {
        "content": content,
        "publishNow": True,
        "platforms": [{"platform": platform, "accountId": account_id}],
    }
    if media_urls:
        payload["mediaItems"] = [{"type": "image", "url": u} for u in media_urls]
    data = await _request("POST", "/v1/posts", json=payload)
    return data or {}


async def get_post(post_id: str) -> dict[str, Any]:
    data = await _request("GET", f"/v1/posts/{post_id}")
    return data or {}
