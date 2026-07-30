import logging
import uuid

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from app.api.deps import CurrentUser, DbSession
from app.config import settings
from app.models.publish import TelegramTarget
from app.schemas.publish import TelegramTargetCreate, TelegramTargetOut, TelegramTargetUpdate
from app.services import secrets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram-targets", tags=["telegram-targets"])


def _normalize_chat_id(raw: str) -> str:
    """Canonicalize whatever the user pasted into a value the Bot API accepts.

    Accepts:
        https://t.me/foo  →  @foo
        http://t.me/foo   →  @foo
        t.me/foo          →  @foo
        @foo              →  @foo
        foo               →  @foo
        -1001234567890    →  -1001234567890   (numeric supergroup/channel)
        1234567890        →  1234567890       (numeric user id)

    Telegram silently returns "chat not found" for any of the URL variants
    (it expects @handle or numeric ID), so doing this once at the boundary
    saves every user from the same paste-the-link mistake.
    """
    s = (raw or "").strip()
    if not s:
        return s
    # Strip protocol + t.me/ prefix variants.
    for prefix in ("https://t.me/", "http://t.me/", "https://telegram.me/", "t.me/", "telegram.me/"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    # Drop any trailing path/query the user pasted (e.g. t.me/foo/123 or ?…).
    s = s.split("/")[0].split("?")[0].strip()
    if not s:
        return s
    # Numeric channel/user IDs stay numeric.
    if s.lstrip("-").isdigit():
        return s
    # Everything else is a username — ensure leading @.
    return s if s.startswith("@") else f"@{s}"


def _derive_public_handle(chat_id: str) -> str | None:
    """Given a normalised chat_id, return a usable public_handle if any.

    Public_handle is consumed by the metrics scraper hitting t.me/<handle>,
    so it only makes sense for @username-style IDs. For numeric supergroup
    IDs we return None (private channel — no public metrics available).
    """
    if chat_id.startswith("@"):
        return chat_id.lstrip("@") or None
    return None


class TelegramBotInfo(BaseModel):
    """Public info about the shared ContentOS Telegram bot — surfaced in the
    frontend so users know which @username to add to their channel as admin."""

    username: str | None
    first_name: str | None
    add_admin_instructions: str


class TelegramVerifyRequest(BaseModel):
    chat_id: str = Field(min_length=1, max_length=255)


class TelegramVerifyResult(BaseModel):
    ok: bool
    chat_title: str | None = None
    chat_type: str | None = None
    member_count: int | None = None
    can_post: bool | None = None
    detail: str | None = None


@router.get("/bot-info", response_model=TelegramBotInfo)
async def bot_info(current: CurrentUser) -> TelegramBotInfo:
    """Return the shared bot's @username so the frontend can show users the
    admin-add instruction. We do `getMe` once per request because the token
    is mutable on the server and we don't want a stale cache to mislead the
    UI after a token rotation. `getMe` is cheap (~50ms) and not on a hot
    path — settings page renders once."""
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        return TelegramBotInfo(
            username=None,
            first_name=None,
            add_admin_instructions=(
                "Бот пока не настроен на сервере. Свяжись с поддержкой."
            ),
        )
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None))
    try:
        me = await bot.get_me()
    except TelegramAPIError as exc:
        logger.warning("telegram getMe failed: %s", exc)
        return TelegramBotInfo(
            username=None,
            first_name=None,
            add_admin_instructions=(
                "Не удалось получить инфу о боте. Попробуй позже."
            ),
        )
    finally:
        await bot.session.close()
    return TelegramBotInfo(
        username=me.username,
        first_name=me.first_name,
        add_admin_instructions=(
            f"1. Открой Telegram-канал, куда хочешь публиковать.\n"
            f"2. Добавь @{me.username} в админы канала с правом «Публикация сообщений».\n"
            f"3. Введи сюда @username канала (если он публичный) или его \n"
            f"   числовой ID вида -100... Затем нажми «Проверить»."
        ),
    )


@router.post("/verify", response_model=TelegramVerifyResult)
async def verify_chat(
    payload: TelegramVerifyRequest, current: CurrentUser
) -> TelegramVerifyResult:
    """Probe Telegram with `getChat` + `getChatMember` to check whether the
    bot has access. This is the diagnostic the user runs BEFORE saving a
    target — saves them the round-trip of saving, trying to publish, and
    seeing «chat not found»."""
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        return TelegramVerifyResult(
            ok=False, detail="Бот не настроен на сервере"
        )
    # Normalize whatever the user pasted into a Bot-API-acceptable form
    # BEFORE hitting Telegram. Otherwise a `https://t.me/foo` paste fails
    # with the generic "chat not found" and the user spends 10 minutes
    # blaming the admin permissions when the problem is the link format.
    chat_id = _normalize_chat_id(payload.chat_id)
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None))
    try:
        chat = await bot.get_chat(chat_id)
        title = chat.title or chat.full_name or None
        # Member-count probe — only on channels/supergroups where the API
        # supports it. Failure is non-fatal; the target is still usable.
        member_count: int | None = None
        try:
            member_count = await bot.get_chat_member_count(chat.id)
        except TelegramAPIError:
            pass
        # Bot admin status — most actionable signal for the user.
        can_post: bool | None = None
        try:
            me = await bot.get_me()
            member = await bot.get_chat_member(chat.id, me.id)
            status_val = getattr(member, "status", None)
            if status_val == "administrator":
                # Aiogram exposes can_post_messages on ChatMemberAdministrator.
                can_post = bool(getattr(member, "can_post_messages", True))
            elif status_val == "creator":
                can_post = True
            else:
                can_post = False
        except TelegramAPIError:
            pass
        return TelegramVerifyResult(
            ok=True,
            chat_title=title,
            chat_type=chat.type,
            member_count=member_count,
            can_post=can_post,
        )
    except TelegramAPIError as exc:
        msg = str(exc)
        # Translate the most common Telegram-side message into something
        # actionable. "chat not found" 99% means the bot isn't added.
        if "chat not found" in msg.lower():
            detail = (
                "Чат не найден. Скорее всего бот не добавлен в канал как "
                "админ — либо chat_id указан неверно."
            )
        elif "forbidden" in msg.lower():
            detail = (
                "Доступ запрещён. Проверь, что бот добавлен в канал как "
                "админ с правом постить."
            )
        else:
            detail = msg
        return TelegramVerifyResult(ok=False, detail=detail)
    finally:
        await bot.session.close()


async def _owned(db, target_id: uuid.UUID, org_id: uuid.UUID) -> TelegramTarget:
    obj = await db.scalar(
        select(TelegramTarget).where(
            TelegramTarget.id == target_id, TelegramTarget.organization_id == org_id
        )
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Target not found")
    return obj


async def _clear_other_defaults(db, org_id: uuid.UUID, except_id: uuid.UUID | None):
    stmt = update(TelegramTarget).where(TelegramTarget.organization_id == org_id)
    if except_id is not None:
        stmt = stmt.where(TelegramTarget.id != except_id)
    await db.execute(stmt.values(is_default=False))


@router.get("", response_model=list[TelegramTargetOut])
async def list_targets(current: CurrentUser, db: DbSession) -> list[TelegramTargetOut]:
    rows = await db.scalars(
        select(TelegramTarget)
        .where(TelegramTarget.organization_id == current.organization_id)
        .order_by(TelegramTarget.is_default.desc(), TelegramTarget.created_at.desc())
    )
    return [TelegramTargetOut.model_validate(r) for r in rows.all()]


@router.post("", response_model=TelegramTargetOut, status_code=status.HTTP_201_CREATED)
async def create_target(
    payload: TelegramTargetCreate, current: CurrentUser, db: DbSession
) -> TelegramTargetOut:
    if payload.is_default:
        await _clear_other_defaults(db, current.organization_id, except_id=None)

    # Accept anything resembling a Telegram chat reference (URL, @handle,
    # bare slug, numeric ID) and store the canonical form so the runtime
    # publish + metrics paths don't have to repeat this logic.
    chat_id = _normalize_chat_id(payload.chat_id)
    explicit_handle = (payload.public_handle or "").strip().lstrip("@")
    public_handle = explicit_handle or _derive_public_handle(chat_id)

    obj = TelegramTarget(
        organization_id=current.organization_id,
        title=payload.title,
        chat_id=chat_id,
        bot_token_encrypted=secrets.encrypt(payload.bot_token),
        is_default=payload.is_default,
        public_handle=public_handle,
    )
    db.add(obj)
    await db.flush()
    return TelegramTargetOut.model_validate(obj)


@router.patch("/{target_id}", response_model=TelegramTargetOut)
async def update_target(
    target_id: uuid.UUID,
    payload: TelegramTargetUpdate,
    current: CurrentUser,
    db: DbSession,
) -> TelegramTargetOut:
    obj = await _owned(db, target_id, current.organization_id)

    data = payload.model_dump(exclude_unset=True)
    if "bot_token" in data:
        new_token = data.pop("bot_token")
        # Empty string on edit means "keep existing token" per the frontend
        # contract; only overwrite when a real value is supplied.
        if new_token:
            obj.bot_token_encrypted = secrets.encrypt(new_token)

    if "chat_id" in data:
        # Same normalisation as on create so post-edit row is in canonical
        # form regardless of how the user typed it the second time.
        data["chat_id"] = _normalize_chat_id(data["chat_id"])
        # If the user cleared public_handle in the same patch we'll respect
        # that below; otherwise auto-fill from the new chat_id.
        if "public_handle" not in data:
            derived = _derive_public_handle(data["chat_id"])
            if derived and not obj.public_handle:
                obj.public_handle = derived

    if "public_handle" in data:
        # Normalise: strip leading '@', treat empty string as NULL so the
        # metrics scraper consistently skips this target.
        raw = (data.pop("public_handle") or "").strip().lstrip("@")
        obj.public_handle = raw or None

    if data.get("is_default") is True:
        await _clear_other_defaults(db, current.organization_id, except_id=obj.id)

    for field, value in data.items():
        setattr(obj, field, value)
    await db.flush()
    return TelegramTargetOut.model_validate(obj)


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target(target_id: uuid.UUID, current: CurrentUser, db: DbSession):
    obj = await _owned(db, target_id, current.organization_id)
    await db.delete(obj)
