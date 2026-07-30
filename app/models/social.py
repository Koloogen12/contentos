"""Подключённые аккаунты площадок — единая таблица для всех провайдеров.

До этого публикация умела ровно одно: Telegram. `PublishLog.target_id`
ссылался прямо на `telegram_targets`, то есть конкретный поставщик был
зашит в схему данных. Добавить Instagram означало бы завести вторую
такую же связь, потом третью.

Здесь аккаунт описан тем, что о нём важно знать продукту: на какой
площадке публикуем и через кого. `provider` — кто доставляет пост:

  * `telegram_bot` — наш собственный бот, работает и ничего не стоит;
  * `linkedin`     — наше приложение LinkedIn, OAuth напрямую;
  * `zernio`       — внешний шлюз для площадок, куда своими силами не
    попасть без ревью Meta или платного тарифа X.

`external_id` — идентификатор аккаунта на стороне провайдера (для Zernio
это `accountId`), `external_profile_id` — их профиль-арендатор, по одному
на организацию. Токенов площадок мы не храним: у Zernio они на их стороне,
у Telegram это токен бота, у LinkedIn — своя таблица.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, uuid_pk


class SocialAccount(Base, TimestampMixin):
    __tablename__ = "social_accounts"
    __table_args__ = (
        # Один и тот же аккаунт площадки не должен подключаться дважды:
        # иначе пост уйдёт в него по числу дублей, а платить придётся за
        # каждый как за отдельный подключённый аккаунт.
        UniqueConstraint(
            "organization_id",
            "provider",
            "external_id",
            name="uq_social_accounts_org_provider_external",
        ),
        Index("idx_social_accounts_org", "organization_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Кто доставляет пост: telegram_bot | linkedin | zernio
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # Куда: instagram | threads | x | telegram | linkedin | ...
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_profile_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Провайдер может сообщить, что доступ протух и нужно переподключить.
    # Отдельно от удаления: аккаунт остаётся, но публиковать в него нельзя.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Сырые данные провайдера — чтобы не заводить колонку под каждое поле,
    # которое понадобится одной площадке.
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
