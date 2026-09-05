"""Celery tasks for scheduled and campaign calling.

All outbound dialing goes through Twilio's default rate limiting — calls beyond
the CPS cap queue on Twilio's side rather than fail. No custom parallel-dialing
infrastructure in Phase 1 (PHASE 2 EXTENSION POINT: a concurrency controller
would slot in around `dial_call`).
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.database import CelerySessionLocal
from app.models import (
    Appointment, AppointmentStatus, BusinessAccount, Call, Campaign, CampaignState,
    Event, EventAccount, FanContact,
)
from app.services import call_orchestrator, number_provisioning, twilio_service
from app.tasks.celery_app import celery


def run(coro):
    """Run an async coroutine from a sync Celery task.

    Celery's default (prefork) worker pool gives each task a plain OS thread
    with no event loop ever created — calling asyncio.get_event_loop() there
    raises RuntimeError in Python 3.10+ before it can even check .is_running().
    asyncio.run() is the correct call for that normal case. The fallback only
    matters if a non-default Celery worker pool (e.g. gevent/eventlet) happens
    to already have a loop running in this thread.
    """
    try:
        return asyncio.run(coro)
    except RuntimeError as exc:
        if "cannot be called from a running event loop" in str(exc):
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(coro)
        raise


@celery.task
def scan_due_appointments():
    async def _scan():
        now = datetime.now(timezone.utc)
        async with CelerySessionLocal() as db:
            due = (
                await db.execute(
                    select(Appointment).where(
                        Appointment.status == AppointmentStatus.scheduled,
                        # scheduled_at is stored timezone-aware; the reminder
                        # fires `reminder_offset_minutes` before it.
                    )
                )
            ).scalars().all()
            for appt in due:
                remind_at = appt.scheduled_at - timedelta(minutes=appt.reminder_offset_minutes)
                if remind_at > now:
                    continue
                account = await db.get(BusinessAccount, appt.business_account_id)
                if account is None or account.status.value != "active":
                    continue
                from_number = await number_provisioning.get_assigned_number(db, "business", account.id)
                if not from_number:
                    continue
                appt.status = AppointmentStatus.reminder_queued
                call = await call_orchestrator.create_appointment_call(db, account, appt, from_number)
                dial_call.delay(str(call.id))
    run(_scan())


@celery.task
def scan_due_campaigns():
    """Mirrors scan_due_appointments — a campaign now waits for its own
    scheduled_at (validated against the event's release_deadline at
    create/update time) rather than being dispatched immediately when
    payment/approval happens. send_campaign's own state guard
    (state not in paid/paused_for_review -> return) already makes this
    safe against double-dispatch if a campaign is ever scanned more than
    once before its state flips away from paid."""
    async def _scan():
        now = datetime.now(timezone.utc)
        async with CelerySessionLocal() as db:
            due = (
                await db.execute(
                    select(Campaign).where(
                        Campaign.state == CampaignState.paid,
                        Campaign.scheduled_at <= now,
                    )
                )
            ).scalars().all()
            for campaign in due:
                send_campaign.delay(str(campaign.id))
    run(_scan())


@celery.task(bind=True, max_retries=3, default_retry_delay=30)
def dial_call(self, call_id: str):
    async def _dial():
        async with CelerySessionLocal() as db:
            call = await db.get(Call, uuid.UUID(call_id))
            if call is None or call.twilio_call_sid:
                return
            sid = twilio_service.place_call(call.to_number, call.from_number, str(call.id))
            call.twilio_call_sid = sid
            call.started_at = datetime.now(timezone.utc)
            await db.commit()
    try:
        run(_dial())
    except Exception as exc:  # transient Twilio/API errors retry with backoff
        raise self.retry(exc=exc)


@celery.task
def send_campaign(campaign_id: str):
    """Fires only after webhook-confirmed payment (or, for a resume, an
    admin clearing the account from review — see
    app/api/internal/risk_review.py). Never called from the checkout
    response path.

    Resumable and idempotent: safe to re-invoke on the same campaign at
    any point. Any fan who already has a Call row for this campaign is
    skipped, so a resume (or any accidental re-invocation) never
    double-dials someone already called.

    Mid-dispatch pause: the owning account's manual_review_required flag
    (Layer 4 — see pipeline.py) is re-checked on every iteration through
    the contact list, not just once at the top, since a long-running
    dispatch can outlast a concurrent Layer-4 feedback-loop tick that
    flags the account partway through. If it trips, the campaign parks at
    `paused_for_review` and stops dispatching further NEW calls — calls
    already dispatched before that moment are untouched and keep
    resolving normally. An admin clearing the account resumes by
    re-invoking this same task."""
    async def _send():
        async with CelerySessionLocal() as db:
            campaign = await db.get(Campaign, uuid.UUID(campaign_id))
            if campaign is None or campaign.state not in (CampaignState.paid, CampaignState.paused_for_review):
                return
            event = await db.get(Event, campaign.event_id)
            fans = (
                await db.execute(select(FanContact).where(FanContact.event_id == event.id))
            ).scalars().all()

            business = await db.get(BusinessAccount, event.business_account_id) if event.business_account_id else None
            event_acct = await db.get(EventAccount, event.event_account_id) if event.event_account_id else None
            owner_type = "business" if business else "event"
            owner = business or event_acct

            if campaign.is_rush_tier:
                # Rush tier uses randomly-assigned pooled numbers, not a fixed one.
                from_number = None
            else:
                from_number = await number_provisioning.get_assigned_number(db, owner_type, owner.id)
                if not from_number:
                    assigned = await number_provisioning.assign_number(db, owner_type, owner.id, owner.country)
                    from_number = assigned.number if assigned else None
            campaign.state = CampaignState.sending
            campaign.total_calls = len(fans)
            await db.commit()

            already_called = set(
                (await db.execute(
                    select(Call.to_number).where(Call.campaign_id == campaign.id)
                )).scalars().all()
            )

            for fan in fans:
                if fan.phone_number in already_called:
                    continue

                # Re-checked every iteration (not just once at the top) —
                # see the task docstring for why.
                await db.refresh(owner)
                if owner.manual_review_required:
                    campaign.state = CampaignState.paused_for_review
                    await db.commit()
                    return

                fn = from_number or await _random_pool_number(db, owner.country)
                if not fn:
                    continue
                call = await call_orchestrator.create_campaign_call(
                    db, campaign, fan.phone_number, fn,
                    business_account=business, event_account=event_acct,
                    language=owner.preferred_language,
                )
                dial_call.delay(str(call.id))

            # Every fan dispatched (or already had been) without the
            # account ever getting flagged — leave it at `sending`;
            # webhooks.py's _store_result flips it to `completed` once
            # every dispatched call actually resolves.
    run(_send())


async def _random_pool_number(db, country: str) -> str | None:
    from sqlalchemy import func as sqlfunc
    from app.models import NumberStatus, PhoneNumberPool
    row = (
        await db.execute(
            select(PhoneNumberPool.number)
            .where(PhoneNumberPool.country == country,
                   PhoneNumberPool.status.in_([NumberStatus.available, NumberStatus.assigned]))
            .order_by(sqlfunc.random())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row