"""Inbound webhooks — the only place paid/suspended state ever changes.

* Twilio: answer (serve TwiML), gather (keypress), recording, status.
* Stripe: invoice.payment_failed (immediate suspension + email),
  invoice.paid (restore + email / receipt), payment_intent.succeeded
  (campaign -> paid -> sending).

Call results are stored, state updated, and pushed to any registered external
webhooks for the owning account.
"""
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.models import (
    Appointment, AppointmentStatus, BusinessAccount, Call, CallResult, Campaign,
    CampaignState, DialOutcome, Event, EventAccount, KeypressResult, RegisteredWebhook,
    VoiceMessage,
)
from app.models.accounts import AccountStatus
from app.services import confirmation_templates as templates
from app.services import notifications, recording_storage, stripe_service, twilio_service
from app.services.risk.content import PRIZE_FRAME_PATTERNS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# --------------------------- Twilio ---------------------------------------

async def _get_call(db: AsyncSession, call_id: str) -> Call:
    try:
        call = await db.get(Call, uuid.UUID(call_id))
    except ValueError:
        call = None
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return call


async def _owner(db: AsyncSession, call: Call):
    if call.business_account_id:
        return await db.get(BusinessAccount, call.business_account_id)
    if call.event_account_id:
        return await db.get(EventAccount, call.event_account_id)
    return None


def _twiml(xml: str) -> Response:
    return Response(content=xml, media_type="application/xml")


@router.post("/twilio/answer/{call_id}")
async def twilio_answer(call_id: str, db: AsyncSession = Depends(get_db)):
    call = await _get_call(db, call_id)
    voice_msgs = False
    message = "Hello, this is a test call from your CallConfirm demo. Everything is working."
    if call.appointment_id:
        appt = await db.get(Appointment, call.appointment_id)
        account = await db.get(BusinessAccount, call.business_account_id)
        voice_msgs = bool(account and account.voice_messaging_addon)
        # scheduled_at is stored in UTC — must be converted to the
        # appointment's OWN timezone before formatting, or the call
        # announces the wrong time to the customer whenever their timezone
        # isn't UTC (a real, confirmed bug: this previously formatted the
        # raw UTC value directly). format_scheduled_time also gives real
        # Japanese-locale phrasing instead of embedding raw English date
        # text ("Friday August 07 at 03:30 AM") in the middle of an
        # otherwise-Japanese sentence.
        local_time = appt.scheduled_at.astimezone(ZoneInfo(appt.timezone))
        message = await templates.render_opening(
            db,
            appt.confirmation_type,
            appt.client_name,
            account.name if account else "",
            templates.format_scheduled_time(local_time, appt.language),
            appt.subject_detail,
            language=appt.language,
        )
        # Identified once at creation time (AI sentence-check step), not
        # recomputed here — avoids adding AI-call latency to this webhook's
        # response window.
        proper_nouns = json.loads(appt.subject_detail_proper_nouns) if appt.subject_detail_proper_nouns else None
    elif call.campaign_id:
        campaign = await db.get(Campaign, call.campaign_id)
        message = campaign.message_template
        proper_nouns = None
        voice_msgs = False  # campaigns don't have the voice-messaging add-on concept
    return _twiml(await twilio_service.build_reminder_twiml(
        db, message, call.language, call.voice_tier, str(call.id), voice_msgs, proper_nouns=proper_nouns,
    ))


@router.post("/twilio/gather/{call_id}")
async def twilio_gather(call_id: str, Digits: str = Form(""), db: AsyncSession = Depends(get_db)):
    call = await _get_call(db, call_id)
    keypress = {
        "1": KeypressResult.confirmed,
        "2": KeypressResult.declined,
        "3": KeypressResult.voice_message,
        "9": KeypressResult.suspicious_report,
    }.get(Digits, KeypressResult.none)

    account = await _owner(db, call)
    if keypress == KeypressResult.voice_message:
        if account and getattr(account, "voice_messaging_addon", False):
            return _twiml(await twilio_service.build_record_twiml(db, str(call.id), call.language, call.voice_tier))
        keypress = KeypressResult.none  # add-on disabled: ignore press 3

    await _store_result(db, call, DialOutcome.answered, keypress)
    from twilio.twiml.voice_response import VoiceResponse
    resp = VoiceResponse()
    if call.appointment_id and keypress in (KeypressResult.confirmed, KeypressResult.declined):
        appt = await db.get(Appointment, call.appointment_id)
        closing = await templates.render_closing(
            db, appt.confirmation_type, confirmed=keypress == KeypressResult.confirmed, language=appt.language,
        )
    else:
        closing = {
            KeypressResult.confirmed: "Thank you, you are confirmed. Goodbye.",
            KeypressResult.declined: "Understood, this has been cancelled. Goodbye.",
            KeypressResult.suspicious_report: "Thank you, this has been reported. Goodbye.",
        }.get(keypress, "Goodbye.")
    resp.say(closing, voice=twilio_service.voice_for(call.language, call.voice_tier), language=call.language)
    return _twiml(str(resp))


@router.post("/twilio/recording/{call_id}")
async def twilio_recording(
    call_id: str,
    RecordingUrl: str = Form(""),
    RecordingDuration: int = Form(0),
    db: AsyncSession = Depends(get_db),
):
    call = await _get_call(db, call_id)
    s3_key = f"recordings/{call.id}.mp3"
    try:
        async with httpx.AsyncClient(auth=(settings.twilio_account_sid, settings.twilio_auth_token)) as client:
            audio = (await client.get(f"{RecordingUrl}.mp3")).content
        recording_storage.save_recording(s3_key, audio)
    except Exception:
        logger.exception("Failed to save voice message recording for call %s", call.id)
        # Metadata row is still recorded below — if this was a transient
        # issue, the export/retention task can retry storage access later;
        # if the storage backend itself is misconfigured, this log line is
        # what actually surfaces that now, instead of silently vanishing.
    db.add(VoiceMessage(
        call_id=call.id, appointment_id=call.appointment_id,
        s3_key=s3_key, duration_seconds=RecordingDuration,
    ))
    await _store_result(db, call, DialOutcome.answered, KeypressResult.voice_message)
    from twilio.twiml.voice_response import VoiceResponse
    resp = VoiceResponse()
    resp.say(
        twilio_service.recording_saved_text(call.language),
        voice=twilio_service.voice_for(call.language, call.voice_tier),
        language=call.language,
    )
    return _twiml(str(resp))


@router.post("/twilio/status/{call_id}")
async def twilio_status(
    call_id: str,
    CallStatus: str = Form(""),
    CallDuration: int = Form(0),
    db: AsyncSession = Depends(get_db),
):
    call = await _get_call(db, call_id)
    call.duration_seconds = CallDuration
    outcome = {
        "completed": DialOutcome.answered,
        "no-answer": DialOutcome.no_answer,
        "busy": DialOutcome.busy,
        "failed": DialOutcome.failed,
    }.get(CallStatus, DialOutcome.failed)
    # _store_result's atomic upsert (see its docstring) correctly handles
    # both cases now — a fresh row if none exists yet, or preserving an
    # already-recorded keypress if one does — so the separate existence
    # check that used to live here is gone; it was itself the other half
    # of the race this whole fix addresses.
    await _store_result(db, call, outcome, KeypressResult.none)

    # Metered billing: report completed billable business calls to Stripe.
    if outcome == DialOutcome.answered and call.billable and call.business_account_id:
        account = await db.get(BusinessAccount, call.business_account_id)
        if account and account.stripe_subscription_id:
            try:
                stripe_service.report_call_usage(account.stripe_subscription_id)
            except Exception:
                pass
        call.cost_usd = settings.price_per_call_usd
        await db.commit()
    return {"ok": True}


async def _store_result(db: AsyncSession, call: Call, outcome: DialOutcome, keypress: KeypressResult):
    # Read the previous keypress BEFORE the upsert, so we can tell whether
    # this call to _store_result is what newly introduced a confirm/decline/
    # no-answer result, or is just a later, redundant call (e.g.
    # /twilio/status firing after /twilio/gather already recorded the real
    # keypress) for a result that was already counted. Without this check,
    # campaign confirmed_count/declined_count/no_answer_count double-
    # increment: Twilio calls both /gather (real keypress) and /status
    # (generic completion) for the same call.
    previous = (
        await db.execute(select(CallResult.keypress).where(CallResult.call_id == call.id))
    ).scalar_one_or_none()

    # Twilio can call /recording and /status for the same call within
    # milliseconds of each other (a call ending naturally triggers both
    # near-simultaneously) — the previous "SELECT, then decide INSERT or
    # UPDATE" pattern had a real race window: two concurrent requests could
    # both see "no row yet" and both attempt to insert, and the second one
    # crashed with a UniqueViolationError (confirmed via live testing — this
    # is what was silently blocking every voice-message recording). An
    # atomic INSERT ... ON CONFLICT DO UPDATE closes that race entirely at
    # the database level: whichever request's insert arrives first wins;
    # the second becomes an update instead of a conflict, no matter how
    # close together they land.
    stmt = pg_insert(CallResult).values(call_id=call.id, dial_outcome=outcome, keypress=keypress)
    stmt = stmt.on_conflict_do_update(
        index_elements=[CallResult.call_id],
        set_={
            "dial_outcome": stmt.excluded.dial_outcome,
            # A generic status-callback fallback (keypress=none) must never
            # clobber an already-recorded, more specific keypress (e.g. a
            # press-3 voice message shouldn't be overwritten by a later
            # "call completed, no keypress" update) — same rule the
            # original code enforced, preserved here.
            "keypress": case(
                (stmt.excluded.keypress != KeypressResult.none, stmt.excluded.keypress),
                else_=CallResult.keypress,
            ),
        },
    ).returning(CallResult.keypress)
    final_keypress = (await db.execute(stmt)).scalar_one()

    # Update appointment / campaign state — using the ACTUAL final persisted
    # keypress read back above, not the local `keypress` parameter or an
    # "else appt.status" fallback. That fallback was a real, confirmed bug:
    # a call that was genuinely answered but where NOTHING was ever pressed
    # (e.g. the recipient hung up mid-message, before Gather's timeout even
    # completed — no /twilio/gather request ever fires in that case) left
    # the appointment stuck at whatever status it already had —
    # "reminder_queued" / "Call in progress" — forever, since the code had
    # no way to tell that case apart from "a more specific keypress-based
    # update already happened via a separate request and shouldn't be
    # downgraded now." Reading back the true final keypress after the
    # atomic upsert removes that ambiguity: if a real keypress was already
    # recorded by an earlier request, it's correctly preserved (the upsert's
    # CASE expression already guarantees that); if nothing was ever
    # recorded, this correctly resolves to no_answer instead of leaving the
    # appointment permanently stuck.
    if call.appointment_id:
        appt = await db.get(Appointment, call.appointment_id)
        appt.status = {
            KeypressResult.confirmed: AppointmentStatus.confirmed,
            KeypressResult.declined: AppointmentStatus.declined,
            KeypressResult.voice_message: AppointmentStatus.voice_message_left,
            KeypressResult.suspicious_report: AppointmentStatus.reported,
        }.get(final_keypress, AppointmentStatus.no_answer)
    if call.campaign_id:
        campaign = await db.get(Campaign, call.campaign_id)
        # Only count on the actual transition into confirmed/declined/
        # no_answer — not on every subsequent webhook call that merely
        # re-confirms an already-recorded result.
        newly_confirmed = final_keypress == KeypressResult.confirmed and previous != KeypressResult.confirmed
        newly_declined = final_keypress == KeypressResult.declined and previous != KeypressResult.declined
        # Same unified vocabulary as Calls/Appointments: keypress == none
        # covers both "never picked up" and "picked up, no response".
        newly_no_answer = final_keypress == KeypressResult.none and previous != KeypressResult.none
        if newly_confirmed:
            campaign.confirmed_count += 1
        elif newly_declined:
            campaign.declined_count += 1
        elif newly_no_answer:
            campaign.no_answer_count += 1

        # Nothing else in the codebase ever moves a campaign out of
        # `sending` — once every dispatched call has a recorded result
        # (regardless of outcome: answered, no_answer, busy, failed all
        # count), the campaign is genuinely done and should show as
        # completed rather than sit at "Sending" forever.
        if campaign.state == CampaignState.sending and campaign.total_calls > 0:
            results_recorded = (
                await db.execute(
                    select(func.count(CallResult.id))
                    .join(Call, Call.id == CallResult.call_id)
                    .where(Call.campaign_id == campaign.id)
                )
            ).scalar_one()
            if results_recorded >= campaign.total_calls:
                campaign.state = CampaignState.completed

        # Safety-net SMS after reward/result-style calls.
        import re
        if any(re.search(p, campaign.message_template.lower()) for p in PRIZE_FRAME_PATTERNS):
            try:
                notifications.send_safety_net_sms(call.to_number, call.from_number)
            except Exception:
                pass
    await db.commit()
    await _push_external_webhooks(db, call, outcome, keypress)


async def _push_external_webhooks(db, call: Call, outcome: DialOutcome, keypress: KeypressResult):
    if not call.business_account_id:
        return
    hooks = (
        await db.execute(
            select(RegisteredWebhook).where(
                RegisteredWebhook.business_account_id == call.business_account_id,
                RegisteredWebhook.active.is_(True),
            )
        )
    ).scalars().all()
    if not hooks:
        return
    payload = json.dumps({
        "call_id": str(call.id),
        "appointment_id": str(call.appointment_id) if call.appointment_id else None,
        "dial_outcome": outcome.value,
        "keypress": keypress.value,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    async with httpx.AsyncClient(timeout=5) as client:
        for hook in hooks:
            sig = hmac.new(hook.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
            try:
                await client.post(hook.url, content=payload,
                                  headers={"Content-Type": "application/json",
                                           "X-CallConfirm-Signature": sig})
            except Exception:
                continue


# --------------------------- Stripe ----------------------------------------

@router.post("/stripe")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe_service.construct_webhook_event(payload, sig)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid signature")

    event_dict = event.to_dict()
    kind = event_dict["type"]
    obj = event_dict["data"]["object"]

    if kind == "invoice.payment_failed":
        account = await _account_by_customer(db, obj.get("customer"))
        if account:
            account.status = AccountStatus.suspended       # immediate suspension
            await db.commit()
            notifications.send_email(
                account.email, "payment_failed",
                link=f"{settings.api_base_url}/billing/update-card",
            )

    elif kind == "invoice.paid":
        account = await _account_by_customer(db, obj.get("customer"))
        if account:
            if account.status in (AccountStatus.suspended, AccountStatus.unpaid):
                account.status = AccountStatus.active      # immediate restoration
                await db.commit()
                notifications.send_email(account.email, "payment_restored")
            else:
                notifications.send_email(
                    account.email, "payment_receipt",
                    amount=f"${obj.get('amount_paid', 0) / 100:.2f}",
                )

    elif kind == "customer.subscription.updated" and obj.get("status") == "unpaid":
        account = await _account_by_customer(db, obj.get("customer"))
        if account:
            account.status = AccountStatus.unpaid          # retries exhausted
            await db.commit()

    elif kind == "payment_intent.succeeded":
        metadata = obj.get("metadata") or {}
        campaign_id = metadata.get("campaign_id")
        if campaign_id:
            campaign = await db.get(Campaign, uuid.UUID(campaign_id))
            if campaign and campaign.state == CampaignState.payment_pending:
                campaign.state = CampaignState.paid        # webhook-driven, only here
                # campaign.stripe_invoice_id was already set at checkout
                # time (create_campaign_invoice_payment creates and
                # finalizes the Invoice synchronously) — nothing to fetch
                # or set here for the receipt/PDF anymore.
                # One-time event-account SIM/number fee: only flips true
                # once payment for a campaign that actually included it is
                # confirmed here — never at checkout time, so an
                # abandoned/failed PaymentIntent never marks it paid.
                if metadata.get("sim_fee_included") == "true":
                    event_row = await db.get(Event, campaign.event_id)
                    if event_row and event_row.event_account_id:
                        event_account = await db.get(EventAccount, event_row.event_account_id)
                        if event_account and not event_account.sim_fee_charged:
                            event_account.sim_fee_charged = True
                await db.commit()
                # Dispatch is no longer immediate here either — the
                # campaign now waits for its own scheduled_at, picked up by
                # scan_due_campaigns once that time arrives.

    return {"received": True}


async def _account_by_customer(db, customer_id: str | None):
    if not customer_id:
        return None
    for model in (BusinessAccount, EventAccount):
        row = (
            await db.execute(select(model).where(model.stripe_customer_id == customer_id))
        ).scalar_one_or_none()
        if row:
            return row
    return None