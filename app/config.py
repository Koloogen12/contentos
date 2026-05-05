from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    APP_ENV: Literal["development", "staging", "production"] = "development"
    APP_DEBUG: bool = True
    LOG_LEVEL: str = "INFO"

    # DB — defaults assume docker-compose network names; override via env in compose.
    DATABASE_URL: str = "postgresql+asyncpg://contentos:contentos@db:5432/contentos"
    DATABASE_SYNC_URL: str = "postgresql+psycopg://contentos:contentos@db:5432/contentos"

    # Redis / queue
    REDIS_URL: str = "redis://redis:6379/0"

    # Auth
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TTL_MINUTES: int = 60
    JWT_REFRESH_TTL_DAYS: int = 30

    # Symmetric encryption for stored secrets (e.g. per-org Telegram bot tokens).
    # Optional — when empty we derive a key from JWT_SECRET (NOT for prod).
    SECRETS_ENCRYPTION_KEY: str = ""

    # AI
    COMETAPI_KEY: str = ""
    COMETAPI_BASE_URL: str = "https://api.cometapi.com/v1"
    COMETAPI_MODEL: str = "claude-sonnet-4-6"
    COMETAPI_MODEL_EMBEDDING: str = "text-embedding-3-small"
    COMETAPI_MODEL_WHISPER: str = "whisper-1"

    # Storage
    S3_ENDPOINT_URL: str = ""
    S3_REGION: str = "ru-1"
    S3_BUCKET: str = "contentos-media"
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""

    # CORS
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    # Limits
    MAX_UPLOAD_SIZE_MB: int = 500
    TEMP_DIR: str = "/tmp/contentos"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
