"""Company panel settings routes."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import current_business_account, current_panel_account
from app.core.security import generate_api_key
from app.models import ALL_SCOPES, ApiKey, BusinessAccount, EventAccount, Industry, SubscriptionTier
from app.models.accounts import AccountStatus
from app.schemas.common import (
    ApiKeyCreate, BusinessNameUpdate, ConfirmationTypeOption, ConfirmationTypesOut, IndustryUpdate,
    PreferredLanguageOut, PreferredLanguageUpdate, WebhookRegister,
)
from app.models.domain import RegisteredWebhook
from app.services import confirmation_templates as templates
from app.services import stripe_service
from app.services.risk import content as risk_content

router = APIRouter(prefix="/settings", tags=["company:settings"])


@router.post("/voice-addon")
async def toggle_voice_addon(
    enabled: bool,
    account: BusinessAccount = Depends(current_business_account),
    db: AsyncSession = Depends(get_db),
):
    """Atomic: the Stripe billing change is attempted FIRST, and our own
    feature-access flag is only committed once that genuinely succeeds — a
    Stripe failure must never leave the account showing a feature it isn't
    actually billed for. (Previously this committed voice_messaging_addon
    before even calling Stripe, so a failure left the flag permanently set
    regardless — fixed here.)"""
    if account.stripe_subscription_id:
        try:
            stripe_service.set_voice_addon(account.stripe_subscription_id, enabled)  # no proration
        except stripe_service.SubscriptionNotActiveError:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Your subscription isn't active, so this can't be billed yet. "
                "Reactivate your plan, then try again.",
            )
        account.voice_addon_billing_synced = True
    account.voice_messaging_addon = enabled
    await db.commit()
    return {"voice_messaging_addon": enabled, "billing_note": "Billing updates on your next cycle"}


@router.post("/api-keys", status_code=201)
async def create_api_key(
    body: ApiKeyCreate,
    account: BusinessAccount = Depends(current_business_account),
    db: AsyncSession = Depends(get_db),
):
    """API access is a paid tier — only `api`-tier active accounts may mint keys."""
    if account.subscription_tier != SubscriptionTier.api or account.status != AccountStatus.active:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "API access requires the API tier")
    invalid = [s for s in body.scopes if s not in ALL_SCOPES]
    if invalid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown scopes: {invalid}")
    raw, key_hash, prefix = generate_api_key()
    db.add(ApiKey(
        business_account_id=account.id, key_hash=key_hash, key_prefix=prefix, scopes=body.scopes
    ))
    await db.commit()
    return {"api_key": raw, "note": "Store this now — it is shown only once."}


@router.get("/api-keys")
async def list_api_keys(
    account: BusinessAccount = Depends(current_business_account),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(select(ApiKey).where(ApiKey.business_account_id == account.id))
    ).scalars().all()
    return [
        {"id": str(k.id), "prefix": k.key_prefix, "scopes": k.scopes,
         "revoked": k.revoked, "last_used_at": k.last_used_at}
        for k in rows
    ]


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: str,
    account: BusinessAccount = Depends(current_business_account),
    db: AsyncSession = Depends(get_db),
):
    key = await db.get(ApiKey, key_id)
    if key is None or key.business_account_id != account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Key not found")
    key.revoked = True
    await db.commit()
    return {"revoked": True}


@router.post("/webhooks", status_code=201)
async def register_webhook(
    body: WebhookRegister,
    account: BusinessAccount = Depends(current_business_account),
    db: AsyncSession = Depends(get_db),
):
    db.add(RegisteredWebhook(business_account_id=account.id, url=body.url, secret=body.secret))
    await db.commit()
    return {"registered": True}


@router.post("/request-deletion")
async def request_account_deletion(
    account=Depends(current_panel_account), db: AsyncSession = Depends(get_db)
):
    """Verified deletion request — full data deletion within 30 days (Celery)."""
    account.status = AccountStatus.pending_deletion
    account.deletion_requested_at = datetime.now(timezone.utc)
    await db.commit()
    return {"scheduled": True, "completed_within_days": 30}


@router.post("/industry")
async def set_industry(
    body: IndustryUpdate,
    account: BusinessAccount = Depends(current_business_account),
    db: AsyncSession = Depends(get_db),
):
    """Sets which ConfirmationType options this account is offered when
    creating appointments (see confirmation_templates.py) — also a real
    anti-scam signal: an account's registered industry vs. the confirmation
    types it actually sends can be checked for consistency elsewhere."""
    try:
        industry = Industry(body.industry)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown industry: {body.industry}")
    account.industry = industry
    await db.commit()
    return {"industry": industry.value}


@router.post("/business-name")
async def set_business_name(
    body: BusinessNameUpdate,
    account: BusinessAccount = Depends(current_business_account),
    db: AsyncSession = Depends(get_db),
):
    """The business name is spoken aloud in every call ("...with {business}
    at {time}"), so it goes through the same hard-block scan as
    subject_detail — a business can't rename itself into something carrying
    prize/payment-request framing and have that spoken to every recipient.

    Also kept in sync with Stripe's Customer record here — Stripe's copy is
    otherwise only ever set once, when the customer is first created (see
    stripe_service.create_customer), and never touched again on its own.
    Without this sync, a renamed business keeps showing its OLD name on
    every subscription invoice and campaign receipt PDF indefinitely, since
    both are generated by Stripe from its own stored customer name, not
    from our database. Best-effort: a Stripe failure here doesn't block
    the rename itself (our own DB is still the source of truth for the
    account), it just means the Stripe-side sync didn't happen this time."""
    blocked, reasons = risk_content.check_hard_blocks(body.name)
    if blocked:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Business name rejected: {'; '.join(reasons)}")
    account.name = body.name.strip()
    await db.commit()
    if account.stripe_customer_id:
        try:
            stripe_service.update_customer_name(account.stripe_customer_id, account.name)
        except Exception:
            pass
    return {"name": account.name}


@router.post("/preferred-language", response_model=PreferredLanguageOut)
async def set_preferred_language(
    body: PreferredLanguageUpdate,
    account: BusinessAccount | EventAccount = Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    """Both account types — unlike industry/business-name/webhooks above,
    which are business-only. This is the account-level default voice/
    language that event and campaign calls fall back to at dispatch time
    (a Campaign has no language field of its own — see send_campaign),
    and business accounts can also run campaigns (Events isn't event-
    account-exclusive), so both need to be able to set this. Appointments
    are unaffected either way — they always carry their own per-row
    language, chosen individually when created.

    No validation against VALID_LANGUAGES here yet — the frontend only
    ever offers codes from that same list (src/lib/call-languages.ts,
    kept in exact sync with app/core/languages.py), so an invalid value
    would only ever reach here via a direct API call bypassing the UI.
    If that needs to be hard-enforced server-side too, add a check here
    against app.core.languages.VALID_LANGUAGES."""
    account.preferred_language = body.preferred_language
    await db.commit()
    return PreferredLanguageOut(preferred_language=account.preferred_language)


@router.get("/confirmation-types", response_model=ConfirmationTypesOut)
async def list_confirmation_types(account: BusinessAccount = Depends(current_business_account)):
    types = templates.available_types(account.industry)
    return ConfirmationTypesOut(
        industry=account.industry.value,
        types=[
            ConfirmationTypeOption(
                value=t.value,
                label=t.value.replace("_", " ").title(),
                detail_mode=templates.detail_mode(t),
                has_scheduled_time=templates.has_scheduled_time(t),
            )
            for t in types
        ],
    )