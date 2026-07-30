import secrets as py_secrets
import uuid
from datetime import datetime, timezone

import jwt
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.auth import Organization, User
from app.models.knowledge import BrandContext
from app.config import settings
from app.schemas.auth import (
    LoginRequest,
    MeResponse,
    OrganizationOut,
    RefreshRequest,
    RegisterRequest,
    ResendCodeRequest,
    TokenPair,
    UserOut,
    VerificationRequiredResponse,
    VerifyEmailRequest,
)
from app.services.auth import (
    decode_token,
    hash_password,
    make_access_token,
    make_org_slug,
    make_refresh_token,
    verify_password,
)
from app.services import email_verification
from app.services import yandex_oauth
from app.services import trial as trial_svc
from app.services.templates_seed import seed_default_templates

router = APIRouter(prefix="/auth", tags=["auth"])


DEFAULT_BRAND_CONTEXT: dict = {
    # Empty defaults — the founder fills these in via Settings → Profile
    # to teach the AI their personal voice / manifesto / taboo. Previously
    # this dict was prefilled with Danil's personal values, which leaked
    # his brand into every new org's settings page. NEW orgs get a blank
    # canvas they own from minute one.
    "author_name": "",
    "author_handle": "",
    "author_bio": "",
    "active_products": "",
    "voice_rules": "",
    "taboo_list": "",
    "manifesto": "",
    "cta_keywords": [],
    "content_pillars": {},
}


async def provision_workspace(
    db: DbSession,
    *,
    email: str,
    password_hash: str | None,
    display_name: str | None,
    organization_name: str | None,
    auth_provider: str,
    yandex_user_id: str | None = None,
    email_verified: bool,
) -> User:
    """Create org + user + brand context + starter templates.

    Shared by password sign-up and Yandex sign-in so the two paths cannot
    drift — a workspace provisioned one way must look identical to the other.
    """
    org_seed = organization_name or email.split("@")[0]
    org = Organization(
        name=organization_name or f"{email.split('@')[0]}'s workspace",
        slug=make_org_slug(org_seed),
    )
    db.add(org)
    await db.flush()

    user = User(
        organization_id=org.id,
        email=email,
        password_hash=password_hash,
        display_name=display_name,
        auth_provider=auth_provider,
        yandex_user_id=yandex_user_id,
        email_verified_at=datetime.now(timezone.utc) if email_verified else None,
    )
    db.add(user)

    db.add(BrandContext(organization_id=org.id, data=DEFAULT_BRAND_CONTEXT))
    await db.flush()

    await seed_default_templates(db, org.id)
    return user


@router.post(
    "/register",
    response_model=VerificationRequiredResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(payload: RegisterRequest, db: DbSession) -> VerificationRequiredResponse:
    """Create an unconfirmed account and mail a confirmation code.

    No tokens are issued here — see `VerificationRequiredResponse`. The client
    continues at `POST /auth/verify-email`.
    """
    email = payload.email.lower()
    existing = await db.scalar(select(User).where(User.email == email))
    if existing is not None:
        # An address stuck unconfirmed would otherwise be unusable forever:
        # the owner cannot register again (409) and cannot log in (unverified).
        # Re-issue the code instead, but only for accounts with no password
        # set yet or the same password — never reveal or overwrite a real one.
        if existing.email_verified_at is None and existing.auth_provider == "password":
            existing.password_hash = hash_password(payload.password)
            if payload.display_name:
                existing.display_name = payload.display_name
            try:
                _, delivered = await email_verification.issue_code(db, existing)
            except email_verification.VerificationError as exc:
                # Still inside the cooldown — the previous code is live.
                if exc.code != "cooldown":
                    raise
                delivered = True
            return _verification_response(email, delivered)
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    user = await provision_workspace(
        db,
        email=email,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
        organization_name=payload.organization_name,
        auth_provider="password",
        email_verified=False,
    )
    _, delivered = await email_verification.issue_code(db, user)
    return _verification_response(email, delivered)


def _verification_response(email: str, delivered: bool) -> VerificationRequiredResponse:
    return VerificationRequiredResponse(
        email=email,
        code_delivered=delivered,
        resend_cooldown_seconds=settings.EMAIL_CODE_RESEND_COOLDOWN_SECONDS,
        code_ttl_minutes=settings.EMAIL_CODE_TTL_MINUTES,
    )


@router.post("/verify-email", response_model=TokenPair)
async def verify_email(payload: VerifyEmailRequest, db: DbSession) -> TokenPair:
    """Confirm the address with the mailed code and sign the user in."""
    user = await db.scalar(select(User).where(User.email == payload.email.lower()))
    if user is None:
        # Same wording as a wrong code: telling them "no such account" here
        # turns this endpoint into an address checker.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверный код.")
    if user.email_verified_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Адрес уже подтверждён. Войди по паролю.")

    try:
        await email_verification.verify_code(db, user, payload.code.strip())
    except email_verification.VerificationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc

    return TokenPair(
        access_token=make_access_token(user_id=user.id, organization_id=user.organization_id),
        refresh_token=make_refresh_token(user_id=user.id),
    )


@router.post("/resend-code", response_model=VerificationRequiredResponse)
async def resend_code(payload: ResendCodeRequest, db: DbSession) -> VerificationRequiredResponse:
    email = payload.email.lower()
    user = await db.scalar(select(User).where(User.email == email))
    # Unknown or already-confirmed address: answer as if a code went out, so
    # this cannot be used to discover who has an account here.
    if user is None or user.email_verified_at is not None:
        return _verification_response(email, True)

    try:
        _, delivered = await email_verification.issue_code(db, user)
    except email_verification.VerificationError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, exc.message) from exc
    return _verification_response(email, delivered)


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, db: DbSession) -> TokenPair:
    user = await db.scalar(select(User).where(User.email == payload.email.lower()))
    # `password_hash` is NULL for Yandex-only accounts — checking it before
    # calling verify_password keeps a passwordless account from being entered
    # with an empty or arbitrary password.
    if user is None or not user.password_hash or not verify_password(
        payload.password, user.password_hash
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account disabled")
    if user.email_verified_at is None:
        # 403 with a machine-readable marker: the frontend sends them to the
        # code step instead of showing "wrong password", which would be a lie.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "email_not_verified",
        )
    return TokenPair(
        access_token=make_access_token(user_id=user.id, organization_id=user.organization_id),
        refresh_token=make_refresh_token(user_id=user.id),
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, db: DbSession) -> TokenPair:
    try:
        decoded = decode_token(payload.refresh_token)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token expired") from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token") from exc

    if decoded.get("type") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong token type")

    user = await db.scalar(select(User).where(User.id == uuid.UUID(decoded["sub"])))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User unavailable")

    return TokenPair(
        access_token=make_access_token(user_id=user.id, organization_id=user.organization_id),
        refresh_token=make_refresh_token(user_id=user.id),
    )


@router.get("/me", response_model=MeResponse)
async def me(current: CurrentUser, db: DbSession) -> MeResponse:
    org = await db.scalar(select(Organization).where(Organization.id == current.organization_id))
    assert org is not None
    return MeResponse(
        user=UserOut.model_validate(current),
        organization=OrganizationOut.model_validate(org),
    )


# ---------------------------------------------------------------------------
# Anonymous preview → register → 24h trial flow
# ---------------------------------------------------------------------------


class PreviewSessionResponse(BaseModel):
    """Response of `POST /auth/preview-session`. Anonymous visitor gets
    JWT tokens for a kind='preview' org with hard caps and no timer."""

    access_token: str
    refresh_token: str
    preview_ai_runs_left: int
    preview_renders_left: int


# Backward-compat alias used by frontend during transition.
TrialSessionResponse = PreviewSessionResponse


async def _create_preview_session(db: DbSession) -> PreviewSessionResponse:
    """Shared implementation for the public `/preview-session` route AND
    the legacy `/trial-session` alias the older frontend still calls."""
    session_id = py_secrets.token_hex(16)
    org = Organization(
        name="Preview workspace",
        slug=f"preview-{session_id[:8]}",
        kind="preview",
        # NO trial_expires_at — preview is timeless, only capped by ops.
        trial_session_id=session_id,
    )
    db.add(org)
    await db.flush()

    placeholder_email = trial_svc.preview_user_email_placeholder(session_id)
    user = User(
        organization_id=org.id,
        email=placeholder_email,
        password_hash=hash_password(py_secrets.token_urlsafe(32)),
        display_name="",
    )
    db.add(user)
    db.add(BrandContext(organization_id=org.id, data=DEFAULT_BRAND_CONTEXT))
    await db.flush()

    await seed_default_templates(db, org.id)

    remaining = trial_svc.preview_remaining(org)
    return PreviewSessionResponse(
        access_token=make_access_token(user_id=user.id, organization_id=org.id),
        refresh_token=make_refresh_token(user_id=user.id),
        preview_ai_runs_left=remaining["ai_runs"],
        preview_renders_left=remaining["renders"],
    )


@router.post(
    "/preview-session",
    response_model=PreviewSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_preview_session(db: DbSession) -> PreviewSessionResponse:
    """Provision an anonymous preview org + user. No timer, hard caps
    (1 canvas, 3 AI ops, 1 render). Frontend calls this once when a
    visitor lands on /try and stores the tokens like a normal login.

    The first successful Format-node run triggers the frontend's
    mandatory register modal — there's no path to keep using the app
    without converting via `/auth/register-preview`.
    """
    return await _create_preview_session(db)


# Legacy alias — older frontend builds still POST here. Keep until
# the new frontend rolls out everywhere; can drop after.
@router.post(
    "/trial-session",
    response_model=PreviewSessionResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
async def start_trial_session_legacy(db: DbSession) -> PreviewSessionResponse:
    return await _create_preview_session(db)


class RegisterPreviewRequest(BaseModel):
    """Body of `POST /auth/register-preview`. Preview user supplies
    their real email + password; we keep ALL their canvases / nodes /
    knowledge items / renders, set kind='trial', and start the 24h
    countdown."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=255)


@router.post(
    "/register-preview",
    response_model=TokenPair,
    status_code=status.HTTP_200_OK,
)
async def register_from_preview(
    payload: RegisterPreviewRequest,
    current: CurrentUser,
    db: DbSession,
) -> TokenPair:
    """Convert a PREVIEW org into a registered TRIAL org.

    Flow boundary: this is the moment the 24h trial countdown starts.
    The user keeps everything from preview (canvases, nodes, generated
    posts) — only the org's `kind` flips, a real email/password is set,
    and `trial_expires_at` is filled in.

    Returns fresh JWT tokens (preserve user_id/organization_id; old
    tokens still work, but rotating on credential change is good
    hygiene).

    Refuses when:
      - org isn't preview (already registered, return 409)
      - email is already taken (409)
    """
    org = await db.scalar(
        select(Organization).where(Organization.id == current.organization_id)
    )
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")
    if org.kind != "preview":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Этот аккаунт уже зарегистрирован — конвертировать нечего",
        )

    existing = await db.scalar(
        select(User).where(User.email == payload.email, User.id != current.id)
    )
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Этот email уже зарегистрирован"
        )

    current.email = payload.email
    current.password_hash = hash_password(payload.password)
    if payload.display_name:
        current.display_name = payload.display_name

    # The transition: preview → trial. 24h timer starts NOW.
    org.kind = "trial"
    org.trial_expires_at = trial_svc.make_trial_window()
    org.trial_session_id = None

    if org.name == "Preview workspace":
        org.name = f"{payload.email.split('@')[0]}'s workspace"
        org.slug = make_org_slug(payload.email.split("@")[0])

    # Sync brand_context.author_name with display_name on first
    # registration. The user typed their name once during signup;
    # making them re-type it in Settings → "ИМЯ" is dumb UX. They
    # can still override later.
    if payload.display_name:
        bc = await db.scalar(
            select(BrandContext).where(BrandContext.organization_id == org.id)
        )
        if bc is not None and not (bc.data or {}).get("author_name"):
            bc.data = {**(bc.data or {}), "author_name": payload.display_name}

    await db.flush()

    # The address is new and unproven, so it stays unconfirmed — but we do NOT
    # interrupt the session: the person is mid-work in the product and kicking
    # them to a code screen here would cost them the thing they just built.
    # They keep working; the code is waiting when they next sign in (login
    # answers `email_not_verified` and the client goes to the code step).
    # Without this the account would be unloggable-into after the session ends.
    try:
        await email_verification.issue_code(db, current)
    except email_verification.VerificationError:
        # Cooldown only — a code is already in flight.
        pass

    # Issue fresh tokens — old tokens still work, but it's cleaner to
    # rotate on a security-significant event like "user just set a real
    # password".
    return TokenPair(
        access_token=make_access_token(user_id=current.id, organization_id=org.id),
        refresh_token=make_refresh_token(user_id=current.id),
    )


# Legacy alias — older frontend builds POST here. Routes to the same
# implementation so anyone with stale Next.js bundles can still convert.
@router.post(
    "/convert-trial",
    response_model=TokenPair,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
async def convert_trial_legacy(
    payload: RegisterPreviewRequest,
    current: CurrentUser,
    db: DbSession,
) -> TokenPair:
    return await register_from_preview(payload, current, db)


# ---------------------------------------------------------------------------
# Yandex sign-in
# ---------------------------------------------------------------------------
#
# Browser round-trip, not an API call: /yandex/start bounces to Yandex,
# /yandex/callback comes back with a code. The callback cannot carry an auth
# header, so CSRF rests entirely on the signed `state` token.
#
# Tokens reach the SPA through the URL fragment (`#access_token=...`), not a
# query string: fragments are not sent to servers and stay out of access logs,
# proxies and Referer headers.


def _frontend_base() -> str:
    """Where to send the browser back to. Falls back to the first CORS origin
    so local dev works without extra configuration."""
    configured = settings.LINKEDIN_POST_CALLBACK_REDIRECT.strip()
    if configured:
        return configured.rstrip("/")
    origins = settings.cors_origins_list
    return (origins[0] if origins else "http://localhost:3000").rstrip("/")


@router.get("/yandex/start", include_in_schema=False)
async def yandex_start(redirect_after: str | None = None) -> RedirectResponse:
    if not yandex_oauth.configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Вход через Яндекс не настроен на сервере",
        )
    state = yandex_oauth.sign_state(redirect_after=redirect_after)
    return RedirectResponse(yandex_oauth.build_authorize_url(state=state))


@router.get("/yandex/callback", include_in_schema=False)
async def yandex_callback(
    db: DbSession,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    base = _frontend_base()

    def fail(reason: str) -> RedirectResponse:
        # Errors go in the query string on purpose — they carry no secrets and
        # the login page needs to read them after a full page load.
        return RedirectResponse(f"{base}/login?yandex_error={reason}")

    if error or not code or not state:
        return fail(error or "cancelled")

    try:
        yandex_oauth.verify_state(state)
    except RuntimeError:
        return fail("bad_state")

    try:
        tokens = await yandex_oauth.exchange_code(code=code)
        info = await yandex_oauth.fetch_userinfo(access_token=tokens["access_token"])
        yandex_id, email, name = yandex_oauth.extract_identity(info)
    except RuntimeError as exc:
        return fail("no_email" if "no_email" in str(exc) else "exchange_failed")

    # Match on the Yandex id first: it survives the person renaming their
    # mailbox, which the address does not.
    user = await db.scalar(select(User).where(User.yandex_user_id == yandex_id))
    if user is None:
        user = await db.scalar(select(User).where(User.email == email))
        if user is not None:
            # Same address signed up with a password earlier — link the two
            # rather than refusing, and treat Yandex as proof of ownership.
            user.yandex_user_id = yandex_id
            if user.email_verified_at is None:
                user.email_verified_at = datetime.now(timezone.utc)
        else:
            user = await provision_workspace(
                db,
                email=email,
                # No password: this account signs in through Yandex. `login`
                # refuses NULL hashes, so it cannot be entered by password
                # until the owner sets one.
                password_hash=None,
                display_name=name,
                organization_name=None,
                auth_provider="yandex",
                yandex_user_id=yandex_id,
                # Yandex vouched for the address, so no code is needed.
                email_verified=True,
            )

    if not user.is_active:
        return fail("disabled")

    await db.flush()

    access = make_access_token(user_id=user.id, organization_id=user.organization_id)
    refresh = make_refresh_token(user_id=user.id)
    target = "/auth/yandex"
    fragment = f"access_token={access}&refresh_token={refresh}"
    payload = yandex_oauth.verify_state(state)
    after = payload.get("redirect_after")
    if after and after.startswith("/"):
        fragment += f"&next={after}"
    return RedirectResponse(f"{base}{target}#{fragment}")
