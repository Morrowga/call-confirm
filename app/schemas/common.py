import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


# --- Auth / registration ---------------------------------------------------

class RegistrationStep1(BaseModel):
    """Wizard step 1: contact details."""
    account_type: str = Field(pattern="^(business|event)$")
    email: EmailStr
    phone_number: str = Field(min_length=7, max_length=20)
    password: str = Field(min_length=10)


class RegistrationStep2(BaseModel):
    """Wizard step 2: profile (country used for numbers + currency display)."""
    account_id: uuid.UUID
    account_type: str = Field(pattern="^(business|event)$")
    name: str
    country: str = Field(min_length=2, max_length=2)
    timezone: str = "UTC"
    preferred_language: str = "en"


class VerifyEmailRequest(BaseModel):
    token: str


class VerifyPhoneRequest(BaseModel):
    account_id: uuid.UUID
    account_type: str
    otp: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    account_type: str = Field(pattern="^(business|event|platform_admin)$")


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr
    account_type: str = Field(pattern="^(business|event)$")


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=10)


# --- Demo / activation -----------------------------------------------------

class DemoCallRequest(BaseModel):
    target_number: str  # server rejects anything != owner's verified number


class ActivateRequest(BaseModel):
    payment_method_id: str
    subscription_tier: str = Field(default="panel", pattern="^(panel|api)$")
    voice_messaging_addon: bool = False


class ChangeTierRequest(BaseModel):
    subscription_tier: str = Field(pattern="^(panel|api)$")


class AddPaymentMethodRequest(BaseModel):
    payment_method_id: str
    set_default: bool = False


# --- Appointments ------------------------------------------------------------

class ProperNoun(BaseModel):
    text: str          # the exact substring as it appears in the rendered sentence
    language: str      # locale code for its own correct pronunciation, e.g. "ja-JP"


class AppointmentCreate(BaseModel):
    client_name: str
    phone_number: str
    scheduled_at: datetime
    timezone: str = "UTC"
    language: str = "en"
    reminder_offset_minutes: int = 24 * 60
    confirmation_type: str = "appointment"
    subject_detail: str | None = Field(default=None, max_length=100)
    # Populated from the chosen SentenceVariant's proper_nouns at submit time —
    # never re-derived server-side, since the AI already identified these
    # once during the check-sentence step.
    subject_detail_proper_nouns: list[ProperNoun] = []


class AppointmentOut(BaseModel):
    id: uuid.UUID
    client_name: str
    phone_number: str
    scheduled_at: datetime
    timezone: str
    language: str
    status: str
    confirmation_type: str
    subject_detail: str | None
    call_duration_seconds: int | None = None  # from the most recent linked Call row, if any
    voice_message_url: str | None = None
    voice_message_duration_seconds: int | None = None

    model_config = {"from_attributes": True}


class ConfirmationTypeOption(BaseModel):
    value: str
    label: str
    detail_mode: str  # "required" | "optional" | "none"
    has_scheduled_time: bool


class ConfirmationTypesOut(BaseModel):
    industry: str
    types: list[ConfirmationTypeOption]


class IndustryUpdate(BaseModel):
    industry: str


class BusinessNameUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


# --- Events / campaigns ----------------------------------------------------

class EventCreate(BaseModel):
    name: str
    release_deadline: datetime


class EventOut(BaseModel):
    id: uuid.UUID
    name: str
    release_deadline: datetime

    model_config = {"from_attributes": True}


class AppointmentCancelOut(BaseModel):
    id: uuid.UUID
    status: str


class PaginatedAppointments(BaseModel):
    items: list[AppointmentOut]
    total: int
    limit: int
    offset: int


class CampaignCreate(BaseModel):
    event_id: uuid.UUID
    title: str
    message_template: str
    rush_tier: bool = False


class SendCampaignRequest(BaseModel):
    scheduled_at: datetime
    timezone: str


class CampaignUpdate(BaseModel):
    title: str
    message_template: str
    rush_tier: bool = False


class CampaignOut(BaseModel):
    id: uuid.UUID
    title: str
    message_template: str
    rush_tier: bool = Field(validation_alias="is_rush_tier")
    scheduled_at: datetime | None
    timezone: str
    state: str
    total_calls: int
    confirmed_count: int
    declined_count: int
    no_answer_count: int
    cost_usd: float

    model_config = {"from_attributes": True, "populate_by_name": True}


class FanContactOut(BaseModel):
    id: uuid.UUID
    name: str | None
    phone_number: str
    source: str  # "public_signup" | "csv_upload" — derived, not a stored column
    created_at: datetime

    model_config = {"from_attributes": True}


class FanContactUpdate(BaseModel):
    name: str | None = None
    phone_number: str


class PaginatedFanContacts(BaseModel):
    items: list[FanContactOut]
    total: int
    limit: int
    offset: int


class ApiKeyCreate(BaseModel):
    scopes: list[str]


class WebhookRegister(BaseModel):
    url: str
    secret: str


class SentenceCheckRequest(BaseModel):
    confirmation_type: str
    client_name: str
    scheduled_at: datetime | None = None
    timezone: str | None = None
    subject_detail: str | None = None
    language: str


class SentenceCheckOut(BaseModel):
    language_match: bool
    detected_language: str | None = None  # populated only when language_match is False
    # Only populated when language_match is True — the generated sentence to
    # confirm and send. A mismatch blocks creation entirely, so there's
    # nothing to generate/show in that case.
    generated: str | None = None
    detail: str | None = None  # underlying corrected detail text, submitted as subject_detail
    proper_nouns: list[ProperNoun] = []


class UploadPreviewRow(BaseModel):
    client_name: str
    phone_number: str
    scheduled_at: datetime
    timezone: str
    language: str
    confirmation_type: str = "appointment"
    subject_detail: str | None = None
    subject_detail_proper_nouns: list[ProperNoun] = []


class UploadPreviewOut(BaseModel):
    summary: str
    errors: list[str]
    rows: list[UploadPreviewRow]


class CsvUploadOut(BaseModel):
    success: bool
    errors: list[str] = []
    rows: list[UploadPreviewRow] = []


class BulkCommitRequest(BaseModel):
    rows: list[UploadPreviewRow]


class BulkCommitOut(BaseModel):
    created: int


class DashboardOverview(BaseModel):
    account_type: str
    status: str
    demo_calls_used: int
    total_calls: int
    # Separate from total_calls (appointment/subscription calls) — event/
    # campaign calls are shown as their own dashboard card, since a
    # business account can have both, and an event account only ever has
    # this one (it has no appointments at all).
    total_event_calls: int
    manual_review_required: bool
    # The account's default call language/voice — always set (chosen at
    # registration Step 2), never None for either account type. Used
    # directly by Settings' "Call language" section, and it's the actual
    # value campaign/event calls fall back to at dispatch time (Campaign
    # has no language field of its own — see send_campaign/
    # create_campaign_call), so surfacing and letting it be edited here
    # matters for both account types, not just event.
    preferred_language: str
    # Billing display fields — None for Event accounts / not-yet-activated
    # Business accounts, rather than omitted, so the frontend has one
    # consistent shape to check against instead of guessing field presence.
    subscription_tier: str | None = None
    voice_messaging_addon: bool | None = None
    free_calls_used: int | None = None
    per_call_rate_usd: float | None = None
    next_billing_date: datetime | None = None
    business_name: str | None = None


class PreferredLanguageUpdate(BaseModel):
    preferred_language: str


class PreferredLanguageOut(BaseModel):
    preferred_language: str