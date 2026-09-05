import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Float, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.accounts import uuid_pk


class NumberStatus(str, enum.Enum):
    available = "available"     # released, reusable within its country
    assigned = "assigned"
    demo = "demo"               # the single shared demo number


class PhoneNumberPool(Base):
    __tablename__ = "phone_number_pool"
    id: Mapped[uuid.UUID] = uuid_pk()
    number: Mapped[str] = mapped_column(String(32), unique=True)
    country: Mapped[str] = mapped_column(String(2), index=True)
    status: Mapped[NumberStatus] = mapped_column(
        Enum(NumberStatus, native_enum=False), default=NumberStatus.assigned, index=True
    )
    twilio_sid: Mapped[str] = mapped_column(String(64))
    # One number = one account at a time. Enforced by this nullable single slot.
    assigned_account_type: Mapped[str | None] = mapped_column(String(16))
    assigned_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purchased_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Which single row (per account) is CURRENTLY used for dispatch. An
    # account can accumulate multiple historical rows over time via a SIM
    # change/additional-number request — every number it has ever used
    # stays permanently linked (assigned_account_* never cleared for a
    # change, only for a full release/cancellation), so past call
    # recipients' caller-ID history stays meaningful and no future account
    # could ever inherit a number with someone else's calling history
    # behind it. Exactly one row per account should ever be True.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Set only when this row exists because of a self-service "request an
    # additional SIM" flow (see billing.py) — links back to the request an
    # admin fulfilled by purchasing this real number. NULL for every
    # normal first-activation number. While set and is_active is still
    # False, this row is "purchased, awaiting the account holder's one-time
    # payment" — not yet usable for dispatch.
    provisioning_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provisioning_requests.id"), index=True
    )


class ProvisioningRequest(Base):
    """Signups that exceeded the auto-purchase cap wait here for manual
    approval — and also, separately, self-service "request an additional
    SIM" requests from an account that already has a number (see
    billing.py's /numbers/request). status widened from 16 to 32 chars to
    fit "ready_for_payment"."""
    __tablename__ = "provisioning_requests"
    id: Mapped[uuid.UUID] = uuid_pk()
    account_type: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    country: Mapped[str] = mapped_column(String(2))
    # pending -> ready_for_payment (admin purchased the real number, linked
    # via PhoneNumberPool.provisioning_request_id, email sent) -> paid
    # (account holder completed the flow — a real $15 charge if
    # is_additional, or a free $0 confirmation if not) | rejected
    status: Mapped[str] = mapped_column(String(32), default="pending")
    # False (default) = this account had zero numbers and hit the
    # auto-purchase cap on its FIRST-ever number — free once fulfilled,
    # same as normal activation would have been. True = a self-service
    # "request an additional SIM" from an account that already has an
    # active number (see billing.py's /numbers/request) — this one is
    # NOT free; settings.additional_sim_fee_usd applies at payment time.
    is_additional: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewStatus(str, enum.Enum):
    pending = "pending"
    cleared = "cleared"
    rejected = "rejected"


class RiskScore(Base):
    __tablename__ = "risk_scores"
    id: Mapped[uuid.UUID] = uuid_pk()
    account_type: Mapped[str | None] = mapped_column(String(16))
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    composite_score: Mapped[int] = mapped_column(Integer)
    factor_breakdown: Mapped[dict] = mapped_column(JSONB, default=dict)
    review_status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, native_enum=False), default=ReviewStatus.pending, index=True
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlatformConfig(Base):
    """Runtime-configurable settings (auto-purchase caps, risk thresholds)."""
    __tablename__ = "platform_config"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DemoNumberUsage(Base):
    """Hourly usage rows for the shared demo number, used to enforce the
    combined calls/hour cap that protects the number from spam-flagging."""
    __tablename__ = "demo_number_usage"
    id: Mapped[uuid.UUID] = uuid_pk()
    hour_bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    calls: Mapped[int] = mapped_column(Integer, default=0)