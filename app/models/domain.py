import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, func
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.accounts import uuid_pk


class AppointmentStatus(str, enum.Enum):
    scheduled = "scheduled"
    reminder_queued = "reminder_queued"
    confirmed = "confirmed"
    declined = "declined"
    voice_message_left = "voice_message_left"
    no_answer = "no_answer"
    cancelled = "cancelled"
    reported = "reported"  # recipient pressed 9 — call completed, flagged suspicious


class ConfirmationType(str, enum.Enum):
    """What the call is confirming — determines both the opening line and
    the two possible closing lines (see app/services/confirmation_templates.py).

    appointment/reservation/meeting/order/delivery are the universal base —
    every industry gets all five, since any business can plausibly need any
    of them. Everything else here is a niche-specific extra, layered on top
    of that base only for the industries where it applies (see
    INDUSTRY_EXTRA_TYPES in confirmation_templates.py).

    Every template is fixed/pre-approved; subject_detail only ever fills a
    noun-phrase slot inside one, never the whole message."""
    # --- Universal base ---
    appointment = "appointment"
    reservation = "reservation"
    meeting = "meeting"
    order = "order"
    delivery = "delivery"
    # --- Niche-specific extras ---
    property_viewing = "property_viewing"
    closing_appointment = "closing_appointment"
    lease_signing = "lease_signing"
    legal_consultation = "legal_consultation"
    deposition = "deposition"
    document_signing = "document_signing"
    court_appearance = "court_appearance"
    jury_duty = "jury_duty"
    government_office = "government_office"
    visa_interview = "visa_interview"
    job_interview = "job_interview"


class Appointment(Base):
    __tablename__ = "appointments"
    id: Mapped[uuid.UUID] = uuid_pk()
    business_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("business_accounts.id"), index=True
    )
    client_name: Mapped[str] = mapped_column(String(255))
    phone_number: Mapped[str] = mapped_column(String(32))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    language: Mapped[str] = mapped_column(String(10), default="en")
    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(AppointmentStatus, native_enum=False), default=AppointmentStatus.scheduled
    )
    reminder_offset_minutes: Mapped[int] = mapped_column(Integer, default=24 * 60)
    confirmation_type: Mapped[ConfirmationType] = mapped_column(
        Enum(ConfirmationType, native_enum=False), default=ConfirmationType.appointment
    )
    subject_detail: Mapped[str | None] = mapped_column(String(100))
    # JSON string: [{"text": "美味しい寿司", "language": "ja-JP"}, ...] — proper
    # nouns (names/places/brands) identified within subject_detail that keep
    # their own correct pronunciation via an SSML <lang> tag, while the rest
    # of the sentence speaks in the call's base selected language. Populated
    # once, at creation time (via the AI sentence-check step), not
    # recomputed at call time — avoids adding AI-call latency to the actual
    # outbound call's webhook response window.
    subject_detail_proper_nouns: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # PHASE 2 EXTENSION POINT: waitlist/slot-refill would attach here
    # (e.g. `waitlist_entry_id` FK + a WaitlistEntry model). Do not build in Phase 1.


class CampaignState(str, enum.Enum):
    draft = "draft"
    payment_pending = "payment_pending"
    paid = "paid"
    sending = "sending"
    # Layer-4 behavioral restriction tripped mid-dispatch: calls already
    # sent before that moment keep resolving normally, but no further
    # NEW calls go out until an admin clears the account (see
    # risk_review.py). Distinct from held_for_review, which is a hold
    # BEFORE anything was ever dispatched.
    paused_for_review = "paused_for_review"
    completed = "completed"
    held_for_review = "held_for_review"     # risk pipeline hold
    rejected = "rejected"
    cancelled = "cancelled"                 # sent, then cancelled before actually dispatching


class Event(Base):
    __tablename__ = "events"
    id: Mapped[uuid.UUID] = uuid_pk()
    # Either an EventAccount OR a BusinessAccount (using the feature via metered billing)
    event_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("event_accounts.id"), index=True, nullable=True
    )
    business_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("business_accounts.id"), index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(255))
    release_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    campaigns: Mapped[list["Campaign"]] = relationship(back_populates="event")
    fans: Mapped[list["FanContact"]] = relationship(back_populates="event")


class FanContact(Base):
    __tablename__ = "fan_contacts"
    id: Mapped[uuid.UUID] = uuid_pk()
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"), index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    phone_number: Mapped[str] = mapped_column(String(32))
    # Set only for rows created via the public self-signup form (the fan
    # explicitly checked a consent box themselves) — null for CSV-uploaded
    # rows, where consent is instead attested by the uploading organizer at
    # the batch level (see BulkUpload.consent_attested). Two different
    # people are making the consent claim in each case, so this is tracked
    # separately rather than conflated into one flag.
    consent_given_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    event: Mapped[Event] = relationship(back_populates="fans")


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[uuid.UUID] = uuid_pk()
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    # When this campaign should actually go out — set only once the
    # organizer clicks Send (or checkout, for event accounts), not at
    # creation time; a draft has no real send time to speak of yet. Must
    # never be later than the owning event's release_deadline, checked
    # wherever this gets set. Stored in UTC like every other scheduled
    # datetime in this codebase; `timezone` is kept so the organizer's own
    # picked time can be redisplayed correctly, the same pattern
    # Appointment.timezone already uses.
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    state: Mapped[CampaignState] = mapped_column(
        Enum(CampaignState, native_enum=False), default=CampaignState.draft
    )
    message_template: Mapped[str] = mapped_column(Text)
    is_rush_tier: Mapped[bool] = mapped_column(Boolean, default=False)
    total_calls: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_count: Mapped[int] = mapped_column(Integer, default=0)
    declined_count: Mapped[int] = mapped_column(Integer, default=0)
    # Same unified vocabulary as Calls/Appointments: covers both "never
    # picked up" and "picked up, no keypress response" — anything that
    # resolved to no real keypress at all.
    no_answer_count: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # A campaign is billed as a real one-off Stripe Invoice (not a bare
    # PaymentIntent) so it gets Stripe's own generated invoice_pdf — the
    # exact same downloadable-PDF mechanism the subscription invoices use
    # (see stripe_service.get_invoice_pdf / get_invoice_pdf_unchecked).
    # Populated at checkout time (create_campaign_invoice_payment), not at
    # webhook time — the invoice itself is created and finalized
    # synchronously during checkout, unlike the payment confirmation
    # which only completes later via webhook.
    stripe_invoice_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    event: Mapped[Event] = relationship(back_populates="campaigns")
    # PHASE 2 EXTENSION POINT: custom concurrent-dialing config would attach here.


class CallEnvironment(str, enum.Enum):
    test = "test"
    production = "production"


class VoiceTier(str, enum.Enum):
    neural = "neural"       # Business accounts
    standard = "standard"   # Event accounts


class Call(Base):
    __tablename__ = "calls"
    id: Mapped[uuid.UUID] = uuid_pk()
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("appointments.id"), index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("campaigns.id"), index=True)
    # Denormalized owner for billing/reporting queries
    business_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("business_accounts.id"), index=True
    )
    event_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("event_accounts.id"), index=True
    )
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    is_event_call: Mapped[bool] = mapped_column(Boolean, default=False)  # internal-report tag
    to_number: Mapped[str] = mapped_column(String(32))
    from_number: Mapped[str] = mapped_column(String(32))
    twilio_call_sid: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    voice_tier: Mapped[VoiceTier] = mapped_column(Enum(VoiceTier, native_enum=False))
    language: Mapped[str] = mapped_column(String(10), default="en")
    cost_usd: Mapped[float | None] = mapped_column(Float)
    billable: Mapped[bool] = mapped_column(Boolean, default=True)  # False during free-50
    environment: Mapped[CallEnvironment] = mapped_column(Enum(CallEnvironment, native_enum=False))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    result: Mapped["CallResult | None"] = relationship(back_populates="call", uselist=False)


class DialOutcome(str, enum.Enum):
    answered = "answered"
    no_answer = "no_answer"
    busy = "busy"
    failed = "failed"


class KeypressResult(str, enum.Enum):
    confirmed = "1"
    declined = "2"
    voice_message = "3"
    suspicious_report = "9"     # recipient-facing protection
    none = "none"


class CallResult(Base):
    __tablename__ = "call_results"
    id: Mapped[uuid.UUID] = uuid_pk()
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id"), unique=True, index=True)
    dial_outcome: Mapped[DialOutcome] = mapped_column(Enum(DialOutcome, native_enum=False))
    keypress: Mapped[KeypressResult] = mapped_column(
        Enum(KeypressResult, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        default=KeypressResult.none,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    call: Mapped[Call] = relationship(back_populates="result")


class VoiceMessage(Base):
    __tablename__ = "voice_messages"
    id: Mapped[uuid.UUID] = uuid_pk()
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id"), index=True)
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("appointments.id"))
    s3_key: Mapped[str] = mapped_column(String(512))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True  # 90-day retention scan
    )


class BulkUpload(Base):
    __tablename__ = "bulk_uploads"
    id: Mapped[uuid.UUID] = uuid_pk()
    account_type: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    kind: Mapped[str] = mapped_column(String(16))           # appointments | fans
    s3_key: Mapped[str] = mapped_column(String(512))
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    valid_rows: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_rows: Mapped[int] = mapped_column(Integer, default=0)
    invalid_rows: Mapped[int] = mapped_column(Integer, default=0)
    column_mapping: Mapped[dict] = mapped_column(JSONB, default=dict)   # AI-inferred mapping
    consent_attested: Mapped[bool] = mapped_column(Boolean, default=False)  # required for fan lists
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RegisteredWebhook(Base):
    """External webhooks that receive call results (public API integrators)."""
    __tablename__ = "registered_webhooks"
    id: Mapped[uuid.UUID] = uuid_pk()
    business_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("business_accounts.id"), index=True
    )
    url: Mapped[str] = mapped_column(String(1024))
    secret: Mapped[str] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True)