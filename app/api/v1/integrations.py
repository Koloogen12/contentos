"""Статус интеграций с площадками — что уже можно подключить, а что нет.

Экран подключений раньше показывал пользователю служебную ошибку вида
«LinkedIn OAuth не сконфигурирован на сервере (нет CLIENT_ID)». Это правда,
но адресована она владельцу продукта, а не тому, кто нажал кнопку: сделать
с ней читатель ничего не может.

Поэтому статус каждой площадки считается на сервере и отдаётся честно:
  * ready        — приложение зарегистрировано, кнопка «Подключить» работает;
  * needs_setup  — код готов, но у сервера нет ключей приложения;
  * planned      — интеграции ещё нет в коде.

`setup_hint` объясняет, что именно нужно сделать, чтобы площадка заработала.
Ключи приложения намеренно живут в серверных секретах, а не в базе: у THE
DRAFT одно приложение на всех пользователей, как у любого SaaS, — иначе
каждому пришлось бы заводить своё приложение в кабинете разработчика
площадки и проходить их ревью.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import CurrentUser
from app.config import settings

router = APIRouter(prefix="/integrations", tags=["integrations"])

IntegrationStatus = Literal["ready", "needs_setup", "planned"]


class IntegrationOut(BaseModel):
    id: str
    name: str
    status: IntegrationStatus
    # Что даёт подключение — одной строкой.
    capability: str
    # Что нужно сделать, чтобы площадка заработала. Пусто, если уже готова.
    setup_hint: str = ""
    # Честное предупреждение о цене или ограничениях площадки.
    caveat: str = ""


@router.get("", response_model=list[IntegrationOut])
async def list_integrations(current: CurrentUser) -> list[IntegrationOut]:
    telegram_ready = bool(settings.TELEGRAM_BOT_TOKEN)
    linkedin_ready = bool(
        settings.LINKEDIN_CLIENT_ID and settings.LINKEDIN_CLIENT_SECRET
    )

    return [
        IntegrationOut(
            id="telegram",
            name="Telegram",
            status="ready" if telegram_ready else "needs_setup",
            capability="Публикация в канал и сбор метрик по постам.",
            setup_hint=(
                ""
                if telegram_ready
                else "На сервере нет TELEGRAM_BOT_TOKEN. Заведите бота через "
                "@BotFather и передайте токен администратору."
            ),
        ),
        IntegrationOut(
            id="linkedin",
            name="LinkedIn",
            status="ready" if linkedin_ready else "needs_setup",
            capability="Публикация от лица профиля и метрики постов.",
            setup_hint=(
                ""
                if linkedin_ready
                else "Нужно приложение LinkedIn: developer.linkedin.com → "
                "Create app, продукт «Share on LinkedIn» и «Sign In with "
                "LinkedIn using OpenID Connect», redirect URI — "
                "https://draft.neurin.tech/api/v1/linkedin/auth/callback. "
                "Client ID и Client Secret из вкладки Auth передайте "
                "администратору: код подключения готов и ждёт только ключи."
            ),
        ),
        IntegrationOut(
            id="x",
            name="X",
            status="planned",
            capability="Публикация тредов и постов.",
            setup_hint=(
                "Нужно приложение на developer.x.com с доступом на запись."
            ),
            caveat=(
                "Запись в X закрыта бесплатным тарифом: публикация доступна "
                "только на платном плане. Пока площадка не подключена, посты "
                "для неё готовятся в канвасе и копируются вручную."
            ),
        ),
        IntegrationOut(
            id="instagram",
            name="Instagram",
            status="planned",
            capability="Публикация каруселей и Reels.",
            setup_hint=(
                "Нужен профессиональный аккаунт Instagram, привязанный к "
                "странице Facebook, и приложение в Meta for Developers с "
                "продуктом Instagram Graph API."
            ),
            caveat=(
                "Meta требует проверки приложения перед выдачей прав на "
                "публикацию — это занимает недели и делается один раз "
                "владельцем продукта."
            ),
        ),
        IntegrationOut(
            id="threads",
            name="Threads",
            status="planned",
            capability="Публикация постов и веток.",
            setup_hint=(
                "Нужно приложение в Meta for Developers с продуктом Threads "
                "API и тот же профессиональный аккаунт, что и для Instagram."
            ),
            caveat=(
                "Threads API живёт по правилам Meta и тоже требует проверки "
                "приложения."
            ),
        ),
    ]
