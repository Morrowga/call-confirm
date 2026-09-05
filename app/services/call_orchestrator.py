"""Creates Call rows with correct billing/tier/environment tagging and hands
dialing off to the Celery task queue (Twilio's own queueing handles CPS limits)."""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import (
    Appointment, BusinessAccount, Call, CallEnvironment, Campaign, EventAccount, VoiceTier,
)

ENV = CallEnvironment.production if settings.is_production else CallEnvironment.test


def _first_month(account: BusinessAccount) -> bool:
    if not account.subscription_started_at:
        return False
    return datetime.now(timezone.utc) < account.subscription_started_at + timedelta(days=31)


async def create_appointment_call(
    db: AsyncSession, account: BusinessAccount, appointment: Appointment, from_number: str
) -> Call:
    # First 50 calls in month one are free: mark non-billable and count them.
    billable = True
    if _first_month(account) and account.free_calls_used < settings.free_calls_first_month:
        account.free_calls_used += 1
        billable = False
    call = Call(
        appointment_id=appointment.id,
        business_account_id=account.id,
        to_number=appointment.phone_number,
        from_number=from_number,
        voice_tier=VoiceTier.neural,           # Business => Neural
        language=appointment.language,
        billable=billable,
        environment=ENV,
    )
    db.add(call)
    await db.commit()
    await db.refresh(call)
    return call


async def create_campaign_call(
    db: AsyncSession,
    campaign: Campaign,
    to_number: str,
    from_number: str,
    *,
    business_account: BusinessAccount | None = None,
    event_account: EventAccount | None = None,
    language: str = "en",
) -> Call:
    call = Call(
        campaign_id=campaign.id,
        business_account_id=business_account.id if business_account else None,
        event_account_id=event_account.id if event_account else None,
        is_event_call=True,                     # tagged separately for reporting
        to_number=to_number,
        from_number=from_number,
        # Event accounts get Standard tier; Business accounts keep Neural.
        voice_tier=VoiceTier.neural if business_account else VoiceTier.standard,
        language=language,
        billable=business_account is not None,  # metered only for business accts
        environment=ENV,
    )
    db.add(call)
    await db.commit()
    await db.refresh(call)
    return call


async def create_demo_call(db: AsyncSession, account, account_type: str) -> Call:
    call = Call(
        business_account_id=account.id if account_type == "business" else None,
        event_account_id=account.id if account_type == "event" else None,
        is_demo=True,
        to_number=account.phone_number,
        from_number=settings.twilio_demo_number,
        voice_tier=VoiceTier.neural if account_type == "business" else VoiceTier.standard,
        language=account.preferred_language,
        billable=False,
        environment=ENV,
    )
    db.add(call)
    await db.commit()
    await db.refresh(call)
    return call


def dispatch_dial(call_id: uuid.UUID) -> None:
    from app.tasks.calling import dial_call
    dial_call.delay(str(call_id))
