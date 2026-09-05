"""Company panel billing: Demo Mode test call, Stage-2 activation, and the
unified one-time campaign payment used by BOTH business and event accounts.

Nothing here flips paid state directly — subscription/campaign state changes are
driven exclusively by Stripe webhooks (see /api/public/v1/webhooks).

Campaigns (the Events feature) are deliberately NOT part of a business
account's monthly subscription or metered-per-call billing — that only ever
applies to appointments. A campaign is always its own one-time, instant,
non-refundable charge, billed as a real one-off Stripe Invoice (see
stripe_service.create_campaign_invoice_payment) — the exact same mechanism
subscription invoices use, so campaign receipts get a genuine Stripe-
generated PDF via get_invoice_pdf_unchecked, not a hand-built approximation.
Priced the same way (tiered + optional rush) whether the owning account is
business or event. Once paid, there is no cancel path — a stuck/abandoned
payment_pending campaign can simply be retried through this same endpoint.

No risk-pipeline evaluation happens on campaign_checkout for either account
type — event-account checkout never had one before this feature existed,
and it was a mistake for it to have been added here at all; see project
history if this resurfaces.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

import stripe

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import current_business_account, current_panel_account
from app.models import (
    BusinessAccount, Campaign, CampaignState, Event, EventAccount, PhoneNumberPool,
    ProvisioningRequest, SubscriptionTier,
)
from app.models.accounts import AccountStatus
from app.schemas.common import ActivateRequest, AddPaymentMethodRequest, ChangeTierRequest, DemoCallRequest
from app.services import (
    call_orchestrator, demo_mode, notifications, number_provisioning, stripe_service,
)

router = APIRouter(prefix="/billing", tags=["company:billing"])


class CampaignCheckoutRequest(BaseModel):
    scheduled_at: datetime
    timezone: str


@router.post("/demo-call")
async def send_demo_call(
    body: DemoCallRequest,
    account: BusinessAccount | EventAccount = Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    """Demo Mode's single capability. Server-side rules in demo_mode.py."""
    await demo_mode.enforce_demo_rules(db, account, body.target_number)
    acct_type = "business" if isinstance(account, BusinessAccount) else "event"
    call = await call_orchestrator.create_demo_call(db, account, acct_type)
    call_orchestrator.dispatch_dial(call.id)
    remaining = settings.demo_lifetime_call_cap - account.demo_calls_used
    return {"call_id": str(call.id), "demo_calls_remaining": remaining}


@router.post("/activate")
async def activate_subscription(
    body: ActivateRequest,
    account: BusinessAccount | EventAccount = Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    """Stage 2: $0 card validation -> subscription (business) or one-time
    SIM fee (event) -> dedicated number for the account's country. First
    50 calls of month one are free for business accounts (tracked on the
    account and excluded from metered usage)."""
    if account.status == AccountStatus.active:
        raise HTTPException(status.HTTP_409_CONFLICT, "Already active")
    if not (account.email_verified and account.phone_verified):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Verify email and phone first")

    is_business = isinstance(account, BusinessAccount)
    acct_type = "business" if is_business else "event"
    if not account.stripe_customer_id:
        account.stripe_customer_id = stripe_service.create_customer(
            account.email, account.name, acct_type, str(account.id)
        )
        await db.commit()

    if not stripe_service.validate_card(account.stripe_customer_id, body.payment_method_id):
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "Card validation failed")

    sim_fee_charged_now = 0.0
    if is_business:
        account.subscription_tier = SubscriptionTier(body.subscription_tier)
        account.voice_messaging_addon = body.voice_messaging_addon
        try:
            account.stripe_subscription_id = stripe_service.create_business_subscription(
                account.stripe_customer_id, body.subscription_tier, body.voice_messaging_addon
            )
        except stripe.error.CardError:
            # The $0 SetupIntent auth above can succeed while a real charge
            # still fails (e.g. insufficient funds) — this must surface as a
            # clear, actionable error, not a subscription left to silently
            # expire 23 hours later (see create_business_subscription for
            # the full incomplete_expired failure mode this replaces).
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED,
                "Your card was declined for the subscription charge. Try a different card.",
            )
        account.subscription_started_at = datetime.now(timezone.utc)
        account.free_calls_used = 0
    else:
        # Event accounts: real, immediate, non-refundable one-time
        # SIM/number setup fee, charged NOW at activation — moved up from
        # first-campaign-checkout time. Guarded by sim_fee_charged so
        # re-running activation (or a retry) never double-charges, and so
        # campaign_checkout's own sim-fee fallback (still in place for
        # accounts activated before this change) correctly sees this as
        # already paid and skips adding it again.
        if not account.sim_fee_charged:
            try:
                stripe_service.charge_event_sim_fee(
                    account.stripe_customer_id, body.payment_method_id, settings.event_sim_fee_usd,
                )
            except stripe.error.CardError:
                raise HTTPException(
                    status.HTTP_402_PAYMENT_REQUIRED,
                    "Your card was declined for the one-time SIM/number setup fee. Try a different card.",
                )
            account.sim_fee_charged = True
            sim_fee_charged_now = settings.event_sim_fee_usd

    account.status = AccountStatus.active
    await db.commit()

    # Number provisioning is not available yet (settings.
    # disable_number_auto_purchase) — the SIM fee above is charged
    # regardless per updated design, since Stripe billing works today even
    # though real Twilio number registration doesn't yet. Business
    # accounts are unaffected; only the event-account purchase attempt is
    # skipped here, intentionally left commented (not deleted) so it's a
    # one-line restore once provisioning is actually available:
    number = None
    if is_business:
        number = await number_provisioning.assign_number(db, acct_type, account.id, account.country)
    # else:
    #     number = await number_provisioning.assign_number(db, acct_type, account.id, account.country)

    return {
        "status": "active",
        "dedicated_number": number.number if number else None,
        "number_pending_manual_approval": number is None,
        "free_calls_first_month": settings.free_calls_first_month if is_business else 0,
        "sim_fee_charged_usd": sim_fee_charged_now,
    }


@router.post("/change-tier")
async def change_tier(
    body: ChangeTierRequest,
    account: BusinessAccount = Depends(current_business_account),
    db: AsyncSession = Depends(get_db),
):
    """Panel <-> API upgrade/downgrade. Atomic: the Stripe billing item swap
    is attempted FIRST, and our own feature-access flag is only committed
    once that genuinely succeeds — a Stripe failure (e.g. the subscription
    isn't active) must never leave the account showing a tier it isn't
    actually billed for. proration_behavior="none" means the price change
    itself only applies on the next invoice either way — no prorated
    mid-cycle charge or credit."""
    if account.status != AccountStatus.active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account must be active to change tier")
    new_tier = SubscriptionTier(body.subscription_tier)
    if new_tier == account.subscription_tier:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Already on the {new_tier.value} tier")
    if not account.stripe_subscription_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "No active subscription to modify")

    try:
        stripe_service.change_subscription_tier(account.stripe_subscription_id, new_tier.value)  # no proration
    except stripe_service.SubscriptionNotActiveError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Your subscription isn't active, so this can't be billed yet. "
            "Reactivate your plan, then try again.",
        )
    account.subscription_tier = new_tier
    await db.commit()
    return {
        "subscription_tier": new_tier.value,
        "billing_note": "This tier's price applies starting your next billing cycle.",
    }


@router.get("/payment-methods")
async def list_payment_methods(account: BusinessAccount | EventAccount = Depends(current_panel_account)):
    """Both account types now have real Stripe customers with saved cards
    — business accounts from subscription activation, event accounts from
    their $3 SIM fee charge at activation (see activate_subscription) and
    from campaign payments. This was previously business-only, which
    silently broke the campaign payment wizard's saved-card picker
    (CampaignPaymentForm) for event accounts: the request 403'd, the
    picker saw zero cards, and fell through to raw fresh-card entry every
    time even when the account clearly had a card on file already."""
    if not account.stripe_customer_id:
        return []
    return stripe_service.list_payment_methods(account.stripe_customer_id)


@router.post("/payment-methods", status_code=201)
async def add_payment_method(
    body: AddPaymentMethodRequest,
    account: BusinessAccount | EventAccount = Depends(current_panel_account),
):
    if not account.stripe_customer_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "No billing customer set up yet")
    try:
        stripe_service.add_payment_method(account.stripe_customer_id, body.payment_method_id, body.set_default)
    except stripe_service.CardValidationError:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "Card could not be validated.")
    return {"success": True}


@router.delete("/payment-methods/{payment_method_id}")
async def remove_payment_method(
    payment_method_id: str,
    account: BusinessAccount | EventAccount = Depends(current_panel_account),
):
    if not account.stripe_customer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment method not found")
    try:
        stripe_service.remove_payment_method(account.stripe_customer_id, payment_method_id)
    except stripe_service.NotOwnedError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment method not found")
    return {"success": True}


@router.post("/payment-methods/{payment_method_id}/default")
async def set_default_payment_method(
    payment_method_id: str,
    account: BusinessAccount | EventAccount = Depends(current_panel_account),
):
    if not account.stripe_customer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment method not found")
    try:
        stripe_service.set_default_payment_method(account.stripe_customer_id, payment_method_id)
    except stripe_service.NotOwnedError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment method not found")
    return {"success": True}


@router.get("/invoices")
async def list_invoices(account: BusinessAccount = Depends(current_business_account)):
    if not account.stripe_customer_id:
        return []
    return stripe_service.list_invoices(account.stripe_customer_id)


@router.get("/invoices/{invoice_id}/pdf")
async def download_invoice_pdf(
    invoice_id: str,
    account: BusinessAccount = Depends(current_business_account),
):
    """Streams the actual PDF back with our own Content-Disposition header —
    a real download, not a redirect to Stripe's hosted invoice page."""
    if not account.stripe_customer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    try:
        pdf_bytes = stripe_service.get_invoice_pdf(account.stripe_customer_id, invoice_id)
    except stripe_service.NotOwnedError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    except ValueError:
        raise HTTPException(status.HTTP_409_CONFLICT, "This invoice has no PDF available yet")
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=invoice_{invoice_id}.pdf"},
    )


@router.post("/invoices/{invoice_id}/pay")
async def pay_invoice(
    invoice_id: str,
    account: BusinessAccount = Depends(current_business_account),
):
    if not account.stripe_customer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    try:
        stripe_service.pay_invoice(account.stripe_customer_id, invoice_id)
    except stripe_service.NotOwnedError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    return {"success": True}


@router.post("/campaigns/{campaign_id}/checkout")
async def campaign_checkout(
    campaign_id: str,
    body: CampaignCheckoutRequest,
    account: BusinessAccount | EventAccount = Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    """Unified one-time campaign payment — used by BOTH business and event
    accounts. This is deliberately separate from a business account's
    monthly subscription/metered billing, which only ever covers
    appointments: a campaign is always its own instant, one-time,
    non-refundable charge, billed as a real one-off Stripe Invoice (see
    stripe_service.create_campaign_invoice_payment) so it gets a genuine
    Stripe-generated PDF, priced the same way (tiered + optional rush)
    regardless of account type. Sets the campaign's scheduled_at here too
    — the schedule and the payment happen in the same step now. Campaign
    only reaches `paid` after the payment_intent.succeeded webhook
    confirms the charge actually went through — never here."""
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    event = await db.get(Event, campaign.event_id)
    is_business = isinstance(account, BusinessAccount)
    owner_id = event.business_account_id if is_business else event.event_account_id
    if owner_id != account.id:      # ownership enforced at API layer
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    if campaign.state not in (CampaignState.draft, CampaignState.payment_pending):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Campaign is {campaign.state.value}")

    # Layer 4 behavioral restriction (see pipeline.py's
    # run_feedback_loop_for_account) — only blocks starting a NEW payment.
    # A campaign already mid-send when the flag trips is handled inside
    # send_campaign itself (pauses to paused_for_review), not here.
    if account.manual_review_required:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your account is under review — new campaign payments can't be started until it's cleared.",
        )

    if body.scheduled_at > event.release_deadline:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Send time can't be after this event's release deadline ({event.release_deadline.isoformat()}).",
        )

    fan_count = await _fan_count(db, event.id)
    if fan_count == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Upload at least one contact before sending")
    if fan_count < settings.min_campaign_contacts and not settings.disable_min_campaign_contacts:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"This event needs at least {settings.min_campaign_contacts} contacts before a campaign can "
            f"be sent (currently {fan_count}).",
        )

    if campaign.is_rush_tier:
        # Server-side rush-tier gate: deadline must be within 1 hour; cap 200.
        window = (event.release_deadline - datetime.now(timezone.utc)).total_seconds() / 60
        if window > settings.rush_tier_window_minutes or window < 0:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Rush tier is only available within 1 hour of the deadline",
            )
        if fan_count > settings.rush_tier_call_cap:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Rush tier is capped at {settings.rush_tier_call_cap} calls",
            )

    if not account.stripe_customer_id:
        account.stripe_customer_id = stripe_service.create_customer(
            account.email, account.name, "business" if is_business else "event", str(account.id)
        )

    # One-time SIM/number fee: only ever applies to event accounts —
    # business accounts already have a number via subscription activation.
    # Temporarily forced to 0 while number auto-purchase itself is off
    # (settings.disable_number_auto_purchase), since charging for a number
    # that can't actually be purchased/assigned yet would be wrong; this
    # re-enables itself automatically once that setting flips back on.
    sim_fee = 0.0
    if not is_business and not account.sim_fee_charged and not settings.disable_number_auto_purchase:
        sim_fee = settings.event_sim_fee_usd

    invoice_id, payment_intent_id, client_secret, amount, sim_fee_included = (
        stripe_service.create_campaign_invoice_payment(
            account.stripe_customer_id, fan_count, str(campaign.id), rush=campaign.is_rush_tier,
            sim_fee_usd=sim_fee,
            # TESTING ONLY: forces a flat test charge instead of real per-call
            # pricing when settings.force_test_campaign_price_usd is set —
            # see config.py. Currently None, so real pricing is active.
            force_price_usd=settings.force_test_campaign_price_usd,
        )
    )
    campaign.stripe_invoice_id = invoice_id
    campaign.stripe_payment_intent_id = payment_intent_id
    campaign.state = CampaignState.payment_pending
    campaign.cost_usd = amount
    campaign.scheduled_at = body.scheduled_at
    campaign.timezone = body.timezone
    await db.commit()

    estimate = notifications.local_estimate(amount, account.country)
    return {
        "client_secret": client_secret,
        "call_count": fan_count,
        "sim_fee_usd": sim_fee_included,
        "amount_usd": estimate["charge_amount_usd"],
        "local_estimate": (
            f'{estimate["local_estimate"]["amount"]:,.2f} {estimate["local_estimate"]["currency"]}'
            if estimate["local_estimate"]["currency"] != "USD" else None
        ),
    }


@router.get("/campaigns/{campaign_id}/receipt")
async def download_campaign_receipt(
    campaign_id: str,
    account: BusinessAccount | EventAccount = Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    """Returns the campaign's REAL Stripe-generated invoice PDF — the
    exact same mechanism download_invoice_pdf above uses for subscription
    invoices — since a campaign is billed as a genuine one-off Stripe
    Invoice (see stripe_service.create_campaign_invoice_payment), not a
    hand-drawn approximation and not a redirect to a hosted page."""
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None or not campaign.stripe_invoice_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Receipt not found")
    event = await db.get(Event, campaign.event_id)
    is_business = isinstance(account, BusinessAccount)
    owner_id = event.business_account_id if is_business else event.event_account_id
    if owner_id != account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Receipt not found")

    try:
        pdf_bytes = stripe_service.get_invoice_pdf_unchecked(campaign.stripe_invoice_id)
    except ValueError:
        raise HTTPException(status.HTTP_409_CONFLICT, "This receipt has no PDF available yet")
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=receipt_{campaign_id}.pdf"},
    )


@router.get("/total-spend")
async def total_spend(
    account: BusinessAccount | EventAccount = Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
    date_from: str | None = None,
    date_to: str | None = None,
):
    """Total spend across an optional date range (inclusive on both ends),
    combining:
      - business accounts: paid subscription invoices (Stripe, via
        stripe_service.sum_paid_invoices) + paid campaigns
      - event accounts: paid campaigns only — no subscription to include

    date_from/date_to are plain "YYYY-MM-DD" strings from the frontend's
    date inputs; omit either for an open-ended range.

    Campaign date filtering uses Campaign.created_at as the best available
    approximation of "when this was paid" — there's no separate paid_at
    timestamp stored today (a campaign is created as a draft, then paid
    via a Stripe webhook sometime after). For the normal create -> pay
    flow that's close enough in practice, but isn't exact; a dedicated
    paid_at column on Campaign would be the correct fix if this ever
    needs to be precise (e.g. a campaign drafted on the 30th but not paid
    until the 1st of the next month would currently count toward the
    30th's period, not the 1st's)."""
    from sqlalchemy import func, select
    is_business = isinstance(account, BusinessAccount)

    parsed_from = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc) if date_from else None
    parsed_to = (
        datetime.fromisoformat(date_to).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        if date_to else None
    )

    col = Event.business_account_id if is_business else Event.event_account_id
    where = [
        col == account.id,
        Campaign.state.in_([CampaignState.paid, CampaignState.sending, CampaignState.completed]),
    ]
    if parsed_from:
        where.append(Campaign.created_at >= parsed_from)
    if parsed_to:
        where.append(Campaign.created_at <= parsed_to)
    campaign_total = (
        await db.execute(
            select(func.coalesce(func.sum(Campaign.cost_usd), 0.0))
            .select_from(Campaign)
            .join(Event, Event.id == Campaign.event_id)
            .where(*where)
        )
    ).scalar_one()

    subscription_total = 0.0
    if is_business and account.stripe_customer_id:
        subscription_total = stripe_service.sum_paid_invoices(
            account.stripe_customer_id, parsed_from, parsed_to
        )

    return {
        "total_usd": round(float(campaign_total) + subscription_total, 2),
        "campaign_spend_usd": round(float(campaign_total), 2),
        "subscription_spend_usd": subscription_total,
    }


@router.get("/event-call-history")
async def event_call_history(
    account: BusinessAccount | EventAccount = Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    """Every campaign this account has ever paid for, for Billing's
    payment history."""
    from sqlalchemy import select
    is_business = isinstance(account, BusinessAccount)
    col = Event.business_account_id if is_business else Event.event_account_id
    rows = (
        await db.execute(
            select(Campaign, Event.name)
            .join(Event, Event.id == Campaign.event_id)
            .where(
                col == account.id,
                Campaign.state.in_([CampaignState.paid, CampaignState.sending, CampaignState.completed]),
            )
            .order_by(Campaign.created_at.desc())
        )
    ).all()
    return [
        {
            "campaign_id": str(c.id),
            "event_name": event_name,
            "title": c.title,
            "amount_usd": c.cost_usd,
            "state": c.state.value,
            "created_at": c.created_at.isoformat(),
        }
        for c, event_name in rows
    ]


async def _fan_count(db, event_id) -> int:
    from sqlalchemy import func, select
    from app.models import FanContact
    return (
        await db.execute(select(func.count(FanContact.id)).where(FanContact.event_id == event_id))
    ).scalar_one()


class PayNewNumberRequest(BaseModel):
    payment_method_id: str


def _account_type_of(account) -> str:
    return "business" if isinstance(account, BusinessAccount) else "event"


@router.get("/numbers")
async def list_my_numbers(
    account: BusinessAccount | EventAccount = Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    """Every number this account has ever owned — historical + active —
    for Settings' radio-select list. A row still awaiting its one-time
    payment (provisioning_request_id set, is_active False) is included
    too, marked as such, so the UI can show it as "pending payment"
    rather than a selectable option."""
    from sqlalchemy import select
    acct_type = _account_type_of(account)
    rows = (
        await db.execute(
            select(PhoneNumberPool)
            .where(
                PhoneNumberPool.assigned_account_type == acct_type,
                PhoneNumberPool.assigned_account_id == account.id,
            )
            .order_by(PhoneNumberPool.purchased_at.desc())
        )
    ).scalars().all()
    return [
        {
            "id": str(r.id),
            "number": r.number,
            "is_active": r.is_active,
            "awaiting_payment": r.provisioning_request_id is not None and not r.is_active,
            "purchased_at": r.purchased_at.isoformat(),
        }
        for r in rows
    ]


@router.post("/numbers/{number_id}/activate")
async def activate_number(
    number_id: str,
    account: BusinessAccount | EventAccount = Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    """Radio-switch between already-owned, already-paid numbers — one
    active at a time. A number still awaiting its one-time payment can't
    be activated this way; it goes through /numbers/pending-request/{id}/pay
    instead, which activates it as part of completing that payment."""
    acct_type = _account_type_of(account)
    target = await db.get(PhoneNumberPool, number_id)
    if (
        target is None
        or target.assigned_account_type != acct_type
        or target.assigned_account_id != account.id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Number not found")
    if target.provisioning_request_id is not None and not target.is_active:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "Complete the one-time payment for this number before activating it.",
        )

    from sqlalchemy import select
    others = (
        await db.execute(
            select(PhoneNumberPool).where(
                PhoneNumberPool.assigned_account_type == acct_type,
                PhoneNumberPool.assigned_account_id == account.id,
                PhoneNumberPool.id != target.id,
            )
        )
    ).scalars().all()
    for row in others:
        row.is_active = False
    target.is_active = True
    await db.commit()
    return {"active_number": target.number}


@router.get("/numbers/pending-request")
async def pending_sim_request(
    account: BusinessAccount | EventAccount = Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    """The account's current open SIM request, if any — lets Settings show
    the right state: nothing / "request pending with our team" / "ready —
    complete payment". Returns null if there's no open request.

    Filtered to is_additional=True only — an old first-number cap-fallback
    request (is_additional=False, from normal activation) is a completely
    separate concern from this self-service "get me another SIM" flow and
    must never block or be confused with it, even if it's still sitting
    unresolved at status='pending' from before an admin got to it."""
    from sqlalchemy import select
    acct_type = _account_type_of(account)
    req = (
        await db.execute(
            select(ProvisioningRequest)
            .where(
                ProvisioningRequest.account_type == acct_type,
                ProvisioningRequest.account_id == account.id,
                ProvisioningRequest.status.in_(["pending", "ready_for_payment"]),
                ProvisioningRequest.is_additional.is_(True),
            )
            .order_by(ProvisioningRequest.created_at.desc())
        )
    ).scalar_one_or_none()
    if req is None:
        return None
    return {
        "id": str(req.id),
        "status": req.status,
        "is_additional": req.is_additional,
        "fee_usd": settings.additional_sim_fee_usd if req.is_additional else 0.0,
        "created_at": req.created_at.isoformat(),
    }


@router.post("/numbers/request", status_code=201)
async def request_additional_number(
    account: BusinessAccount | EventAccount = Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    """Self-service "request a new SIM" from Settings. NOT auto-purchased
    and NOT charged here — creates a ProvisioningRequest for manual admin
    fulfillment (see number_pool.py's approve endpoint), same queue the
    auto-purchase-cap fallback already uses. Always is_additional=True
    here, since this endpoint is only reachable by an account that
    already has a number — the $15 fee applies once admin fulfills it and
    the account holder pays.

    The "already have one open" check is filtered to is_additional=True
    for the same reason as pending_sim_request above — an old, unresolved
    first-number fallback request must never block a genuinely new
    additional-SIM request."""
    from sqlalchemy import select
    acct_type = _account_type_of(account)
    existing = (
        await db.execute(
            select(ProvisioningRequest).where(
                ProvisioningRequest.account_type == acct_type,
                ProvisioningRequest.account_id == account.id,
                ProvisioningRequest.status.in_(["pending", "ready_for_payment"]),
                ProvisioningRequest.is_additional.is_(True),
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "You already have an open SIM request.")
    db.add(ProvisioningRequest(
        account_type=acct_type, account_id=account.id, country=account.country, is_additional=True,
    ))
    await db.commit()
    return {"requested": True}


@router.post("/numbers/pending-request/{request_id}/pay")
async def pay_for_new_number(
    request_id: str,
    body: PayNewNumberRequest,
    account: BusinessAccount | EventAccount = Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    """Completes a ready_for_payment request. If is_additional (the
    normal self-service case), this is a REAL $15 charge. If not (the
    rare case of a brand-new account's first-ever number having been
    routed through manual approval due to the auto-purchase cap, not a
    self-service request at all), this confirms/activates at $0 — that
    first number was always meant to be free, same as normal activation.
    Either way, deactivates whatever number was active before (if any)
    and activates the new one."""
    acct_type = _account_type_of(account)
    req = await db.get(ProvisioningRequest, request_id)
    if (
        req is None
        or req.account_type != acct_type
        or req.account_id != account.id
        or req.status != "ready_for_payment"
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No number ready for payment")

    from sqlalchemy import select
    number_row = (
        await db.execute(
            select(PhoneNumberPool).where(PhoneNumberPool.provisioning_request_id == req.id)
        )
    ).scalar_one_or_none()
    if number_row is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "No number linked to this request")

    if req.is_additional:
        if not account.stripe_customer_id:
            account.stripe_customer_id = stripe_service.create_customer(
                account.email, account.name, acct_type, str(account.id)
            )
        try:
            stripe_service.charge_additional_sim_fee(
                account.stripe_customer_id, body.payment_method_id, settings.additional_sim_fee_usd,
            )
        except stripe.error.CardError:
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED,
                "Your card was declined for the one-time SIM fee. Try a different card.",
            )

    others = (
        await db.execute(
            select(PhoneNumberPool).where(
                PhoneNumberPool.assigned_account_type == acct_type,
                PhoneNumberPool.assigned_account_id == account.id,
                PhoneNumberPool.id != number_row.id,
            )
        )
    ).scalars().all()
    for row in others:
        row.is_active = False
    number_row.is_active = True
    req.status = "paid"
    req.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    return {"number": number_row.number, "amount_charged_usd": settings.additional_sim_fee_usd if req.is_additional else 0.0}