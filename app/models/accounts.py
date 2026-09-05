import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, func
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class AccountStatus(str, enum.Enum):
    demo = "demo"                 # verified, no card — Demo Mode
    active = "active"             # card validated, subscription active
    suspended = "suspended"       # payment failure or admin/risk suspension
    unpaid = "unpaid"             # Stripe retries exhausted
    pending_deletion = "pending_deletion"
    deleted = "deleted"


class SubscriptionTier(str, enum.Enum):
    panel = "panel"               # $5/mo
    api = "api"                   # $10/mo — unlocks external API keys


class Industry(str, enum.Enum):
    """Filters which ConfirmationType options a Business account is offered
    (see app/services/confirmation_templates.py) — both a UX narrowing (a
    law firm never sees "order"/"delivery" cluttering their form) and a real
    signal for the risk pipeline (a registered law_firm account suddenly
    sending "order" confirmations is inconsistent with its declared
    identity, feeding into the same scoring used for volume/content
    anomalies elsewhere)."""
    general = "general"
    medical = "medical"
    real_estate = "real_estate"
    law_firm = "law_firm"
    government = "government"
    retail = "retail"
    hospitality = "hospitality"
    recruiting = "recruiting"
    education = "education"


class AccountBase:
    """Shared columns. Business and Event accounts are *structurally separate*
    tables per spec — this mixin only avoids column duplication."""
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    phone_number: Mapped[str] = mapped_column(String(32))          # E.164
    country: Mapped[str] = mapped_column(String(2))                # ISO 3166-1 alpha-2
    preferred_language: Mapped[str] = mapped_column(String(10), default="en")

    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    phone_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus, native_enum=False), default=AccountStatus.demo
    )

    # Demo Mode bookkeeping (server-side enforced)
    demo_calls_used: Mapped[int] = mapped_column(Integer, default=0)
    demo_last_call_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Post-call behavioral feedback loop (risk layer 4)
    manual_review_required: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BusinessAccount(AccountBase, Base):
    __tablename__ = "business_accounts"
    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(255))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    subscription_tier: Mapped[SubscriptionTier] = mapped_column(
        Enum(SubscriptionTier, native_enum=False), default=SubscriptionTier.panel
    )
    voice_messaging_addon: Mapped[bool] = mapped_column(Boolean, default=False)
    # Addon billing changes apply next cycle; access changes immediately.
    voice_addon_billing_synced: Mapped[bool] = mapped_column(Boolean, default=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64))
    subscription_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    free_calls_used: Mapped[int] = mapped_column(Integer, default=0)
    industry: Mapped[Industry] = mapped_column(Enum(Industry, native_enum=False), default=Industry.general)

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="business_account")


class EventAccount(AccountBase, Base):
    __tablename__ = "event_accounts"
    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(255))
    # One-time $3 SIM/number fee, charged on this account's first-ever
    # campaign checkout — event accounts have no subscription to fold a
    # phone-number cost into (business accounts already have theirs
    # covered via subscription). Only flips true from the Stripe webhook
    # once that specific payment is actually confirmed (see
    # webhooks.py's payment_intent.succeeded handler) — never at checkout
    # time itself, so an abandoned/failed PaymentIntent can't mark it paid.
    sim_fee_charged: Mapped[bool] = mapped_column(Boolean, default=False)


class PlatformAdmin(Base):
    __tablename__ = "platform_admins"
    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VerificationToken(Base):
    """Email confirmation links, phone OTPs, and password reset tokens."""
    __tablename__ = "verification_tokens"
    id: Mapped[uuid.UUID] = uuid_pk()
    account_type: Mapped[str] = mapped_column(String(16))   # business | event
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    purpose: Mapped[str] = mapped_column(String(32))        # email_verify | phone_otp | password_reset
    token_hash: Mapped[str] = mapped_column(String(128), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[uuid.UUID] = uuid_pk()
    business_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("business_accounts.id"), index=True
    )
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(16))     # e.g. "sk_live_a1b2" for display
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    business_account: Mapped[BusinessAccount] = relationship(back_populates="api_keys")


SCOPE_APPOINTMENTS_READ = "appointments:read"
SCOPE_APPOINTMENTS_WRITE = "appointments:write"
SCOPE_EVENTS_CREATE = "events:create"
ALL_SCOPES = [SCOPE_APPOINTMENTS_READ, SCOPE_APPOINTMENTS_WRITE, SCOPE_EVENTS_CREATE]