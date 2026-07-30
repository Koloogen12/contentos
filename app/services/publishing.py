"""Публикация поста: выбор доставщика по площадке.

Это тот самый слой, ради которого затевался переход на внешний шлюз.
Продукт спрашивает «опубликуй этот текст в этот аккаунт» и не знает, кто
именно доставит: наш телеграм-бот, наше приложение LinkedIn или Zernio.
Смена поставщика — правка одной ветки здесь, а не переписывание публикации,
плана и канваса.

Это не абстракция впрок: поставщик уже менялся один раз на нашей памяти —
getlate.dev стал Zernio и переписал тарифы, — и вероятность второго раза
выше, чем хотелось бы.

Распределение площадок сознательное, а не «всё через шлюз»:

  telegram   → свой бот. Работает, стоит ноль.
  linkedin   → своё приложение. Регистрируется за час, ревью не нужно.
  instagram  → шлюз. Иначе App Review у Meta: недели и право на отказ.
  threads    → шлюз. Та же причина.
  x          → шлюз. Иначе платный тариф X фиксированной платой.

Платим мы и за каждый подключённый аккаунт, поэтому в шлюз уходит только
то, что своими силами дорого или невозможно.
"""
from __future__ import annotations

from typing import Any

from app.services import zernio

# Площадка → кто доставляет. Единственное место, где это записано.
PLATFORM_PROVIDER: dict[str, str] = {
    "telegram": "telegram_bot",
    "linkedin": "linkedin",
    "instagram": "zernio",
    "threads": "zernio",
    "x": "zernio",
}

# Площадки, аккаунт которых подключается через внешний шлюз.
GATEWAY_PLATFORMS = tuple(
    p for p, prov in PLATFORM_PROVIDER.items() if prov == "zernio"
)


class PublishError(RuntimeError):
    """Ошибка публикации с текстом, пригодным для показа пользователю."""


def provider_for(platform: str) -> str:
    provider = PLATFORM_PROVIDER.get(platform)
    if provider is None:
        raise PublishError(f"Публикация в «{platform}» не поддерживается")
    return provider


def is_platform_available(platform: str) -> bool:
    """Можно ли сейчас подключить и использовать площадку."""
    provider = PLATFORM_PROVIDER.get(platform)
    if provider == "zernio":
        return zernio.is_configured()
    # Телеграм и LinkedIn проверяются своими настройками в integrations.py —
    # здесь только про шлюз, остальное всегда «зависит от ключей площадки».
    return True


async def publish_via_gateway(
    *,
    platform: str,
    account_external_id: str,
    text: str,
    media_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Отправить пост через внешний шлюз.

    Телеграм и LinkedIn публикуются своими путями и сюда не попадают — у
    них уже есть рабочие задачи в воркере со своей обработкой ошибок и
    сбором метрик.
    """
    if provider_for(platform) != "zernio":
        raise PublishError(
            f"«{platform}» публикуется не через шлюз — это ошибка вызова"
        )
    try:
        return await zernio.publish(
            platform=platform,
            account_id=account_external_id,
            content=text,
            media_urls=media_urls,
        )
    except zernio.ZernioError as exc:
        raise PublishError(str(exc)) from exc
