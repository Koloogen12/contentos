import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=255)
    organization_name: str | None = Field(default=None, max_length=255)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class VerificationRequiredResponse(BaseModel):
    """Answer to `POST /auth/register`.

    Deliberately carries no tokens: the account is not usable until the address
    is confirmed, so handing out credentials here would let anyone work under
    an address they don't own.

    `code_delivered=False` means SMTP is not configured on the server and the
    code went to the log instead — the frontend says so plainly rather than
    telling the user to check a mailbox nothing was sent to.
    """

    email: str
    verification_required: bool = True
    code_delivered: bool
    resend_cooldown_seconds: int
    code_ttl_minutes: int


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4, max_length=12)


class ResendCodeRequest(BaseModel):
    email: EmailStr


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    # Plain `str`, not `EmailStr` — trial users have server-generated
    # placeholder addresses like `trial-<id>@trial.contentos.local` that
    # use a reserved TLD and fail EmailStr's strict validation. The
    # INCOMING email on register/convert is still validated as EmailStr.
    email: str
    display_name: str | None
    is_active: bool
    created_at: datetime


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    # Trial-mode fields — `kind` distinguishes anonymous trial orgs from
    # registered ones; the other fields surface live quota state for the
    # trial-badge UI. Regular orgs have `kind='regular'` and the trial
    # fields are all null / 0.
    kind: str = "regular"  # "regular" | "trial"
    trial_expires_at: datetime | None = None
    trial_ai_runs_used: int = 0
    trial_renders_used: int = 0
    created_at: datetime


class MeResponse(BaseModel):
    user: UserOut
    organization: OrganizationOut
