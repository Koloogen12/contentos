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
    # Single-model strategy (chosen 2026-07-17, replaces the 2026-05-15
    # two-model split): `claude-opus-4-8` for BOTH creative and structured
    # work.
    #
    # Why this supersedes the old "why not Claude" note: that ruling was
    # about `claude-sonnet-4-5` / `claude-sonnet-4-6`, which CometAPI
    # proxied in an agentic flavour returning `[tool_use_block]` stubs
    # instead of the JSON we asked for. Re-tested on 2026-07-17:
    # opus-4-8 through the same proxy returns clean, unfenced JSON with
    # no tool-use drift, so the exclusion no longer applies.
    #
    # CAUTION: opus-4-8 REJECTS the `temperature` param outright
    # ("`temperature` is deprecated for this model") — every call site
    # still passes one, so `ai_client` strips it for models listed in
    # MODELS_WITHOUT_TEMPERATURE. Do not "simplify" that away.
    #
    # Why not `gpt-5` / `gpt-5-mini`: those are reasoning models — they
    # burn the `max_tokens` budget on internal thinking and return
    # `finish_reason="length"` with empty content for non-trivial prompts.
    # Anthropic напрямую — для всей генерации текста. Причина перехода не
    # цена (у прокси была скидка), а кэширование промпта: в каждый запрос
    # уходит несколько тысяч токенов неизменного контекста — бренд, голос,
    # редполитика, — и прокси кэш не поддерживает. Проверено: там
    # cached_tokens остаётся нулём на повторных запросах.
    # "cometapi" | "anthropic". По умолчанию прокси: API Anthropic не
    # принимает запросы с российских адресов, а прод стоит в Москве.
    # Переключить на "anthropic" можно, когда появится релей в разрешённой
    # стране — код для прямого доступа готов и лежит рядом.
    AI_PROVIDER: str = "cometapi"
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-5"
    # Релей: Cloudflare Worker (tools/anthropic-proxy-worker), т.к. сам
    # api.anthropic.com 403-ит запросы с российских IP. Пусто → SDK идёт
    # напрямую в api.anthropic.com (дев-машины не в РФ). Задан → SDK шлёт
    # запросы на этот URL, а Worker сам подставляет x-api-key и форвардит.
    ANTHROPIC_BASE_URL: str = ""
    # Заголовок x-proxy-key к каждому запросу через релей — свой секрет
    # (сверяется воркером), не путать с ANTHROPIC_API_KEY. Нужен только
    # если задан ANTHROPIC_BASE_URL.
    ANTHROPIC_PROXY_KEY: str = ""

    # Прокси остаётся для того, чего у Anthropic нет: эмбеддинги для поиска
    # похожих образцов голоса, распознавание речи и генерация картинок.
    COMETAPI_MODEL: str = "claude-sonnet-5"
    COMETAPI_MODEL_STRUCTURED: str = "claude-sonnet-5"
    COMETAPI_MODEL_EMBEDDING: str = "text-embedding-3-small"
    COMETAPI_MODEL_WHISPER: str = "whisper-1"
    # Image generation for carousel covers and full AI-carousel slides.
    # `gpt-image-1` is the OpenAI image model, available through the same
    # CometAPI proxy we already use for chat completions. Returns base64
    # PNGs in the response — see `app/services/images.py` for the call.
    COMETAPI_MODEL_IMAGE: str = "gpt-image-1"

    # Storage
    S3_ENDPOINT_URL: str = ""
    S3_REGION: str = "ru-1"
    S3_BUCKET: str = "contentos-media"
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    # Public base URL of our own API host. Used by `services.storage` to
    # build `/api/v1/media/<key>` redirector links that wrap S3 access in
    # a presigned URL (Selectel's public bucket type doesn't expose
    # anonymous-read; we have to sign every read). Leave empty to fall
    # back to the first CORS origin — works in dev and prod when frontend
    # and API share a host.
    S3_PUBLIC_URL_BASE: str = ""

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""

    # LinkedIn OAuth — single org-wide app credentials (Sprint 2 Track C).
    # Register at https://developer.linkedin.com/. The redirect URI MUST
    # match what's configured in the LinkedIn app exactly, including the
    # path. For prod: https://api.draft.neurin.tech/api/v1/linkedin/auth/callback
    # For local dev: http://localhost:8000/api/v1/linkedin/auth/callback
    # Scopes: "openid profile email" for sign-in / identity, "w_member_social"
    # for the eventual publish-on-behalf-of-user flow (next sub-track).
    LINKEDIN_CLIENT_ID: str = ""
    LINKEDIN_CLIENT_SECRET: str = ""
    LINKEDIN_REDIRECT_URI: str = ""
    LINKEDIN_SCOPES: str = "openid profile email w_member_social"
    # Where the OAuth callback redirects the browser after a successful
    # token exchange. The frontend reads ?linkedin=connected / =error to
    # show a toast. Defaults to the CORS first origin so dev "just works".
    LINKEDIN_POST_CALLBACK_REDIRECT: str = ""

    # Yandex OAuth — sign-in with a Yandex account.
    # Register the app at https://oauth.yandex.ru/client/new, tick the
    # "Yandex ID: email address" and "...login and name" permissions, and put
    # the callback below into "Redirect URI" verbatim.
    # Prod:  https://draft.neurin.tech/api/v1/auth/yandex/callback
    # Local: http://localhost:8000/api/v1/auth/yandex/callback
    # Шлюз публикации в Instagram / Threads / X. Ключ вида sk_...
    # Пусто = площадки показываются как неподключённые, ошибок не бросаем.
    ZERNIO_API_KEY: str = ""

    YANDEX_CLIENT_ID: str = ""
    YANDEX_CLIENT_SECRET: str = ""
    YANDEX_REDIRECT_URI: str = ""

    # Outgoing mail — used for the sign-up confirmation code. Sent through
    # Resend (see services/mailer.py for why, rather than SMTP). The same
    # Resend account already serves slotix on this host and has `neurin.tech`
    # verified, so MAIL_FROM only has to be an address on that domain.
    # Leave RESEND_API_KEY empty in dev: the code is then written to the
    # server log instead of being mailed, so the flow stays testable.
    RESEND_API_KEY: str = ""
    MAIL_FROM: str = ""
    MAIL_FROM_NAME: str = "THE DRAFT"

    # Sign-up confirmation code policy.
    EMAIL_CODE_TTL_MINUTES: int = 15
    EMAIL_CODE_MAX_ATTEMPTS: int = 5
    EMAIL_CODE_RESEND_COOLDOWN_SECONDS: int = 60

    # CORS
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    # Limits
    MAX_UPLOAD_SIZE_MB: int = 500
    TEMP_DIR: str = "/tmp/contentos"

    # Public-content fetch proxy. Used by `voice_importers` to reach hosts
    # that are blocked outbound from the production VPS (e.g. Russian-region
    # hosts can't open TCP to t.me). When set, requests go through:
    #   `<VOICE_FETCH_PROXY_URL>?url=<encoded-target-url>`
    # which is the Cloudflare-Worker fetch-relay contract (no auth, returns
    # the upstream body unchanged with an `x-final-url` header). Leave empty
    # for local dev to fetch directly.
    VOICE_FETCH_PROXY_URL: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
