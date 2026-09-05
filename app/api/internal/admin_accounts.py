"""Platform admin: account management. Every route re-checks platform_admin."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import require_platform_admin
from app.models import (
    BusinessAccount, Call, CallResult, Campaign, Event, EventAccount, KeypressResult, PhoneNumberPool,
)
from app.models.accounts import AccountStatus
from app.services import stripe_service

router = APIRouter(prefix="/accounts", tags=["internal:accounts"])

_MODEL = {"business": BusinessAccount, "event": EventAccount}


@router.get("")
async def list_accounts(
    account_type: str = "business",
    _admin=Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = 100, offset: int = 0,
):
    model = _MODEL.get(account_type)
    if model is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "account_type must be business|event")
    rows = (await db.execute(select(model).limit(min(limit, 500)).offset(offset))).scalars().all()
    return [
        {"id": str(a.id), "name": a.name, "email": a.email, "country": a.country,
         "status": a.status.value, "manual_review_required": a.manual_review_required}
        for a in rows
    ]


async def _outcome_counts(db: AsyncSession, where_clauses: list) -> dict:
    """Confirmed/declined/no_answer counts for a filtered set of Call rows.
    no_answer is the residual (total - confirmed - declined) rather than a
    separately-filtered count — matches the same "everything else falls
    into no_answer" semantics used by the company panel's own status
    mapping (see dashboard.py's _call_status), without needing to
    replicate its exact edge-case branching here."""
    total = (
        await db.execute(select(func.count(Call.id)).where(*where_clauses))
    ).scalar_one()
    confirmed = (
        await db.execute(
            select(func.count(Call.id))
            .join(CallResult, CallResult.call_id == Call.id)
            .where(*where_clauses, CallResult.keypress == KeypressResult.confirmed)
        )
    ).scalar_one()
    declined = (
        await db.execute(
            select(func.count(Call.id))
            .join(CallResult, CallResult.call_id == Call.id)
            .where(*where_clauses, CallResult.keypress == KeypressResult.declined)
        )
    ).scalar_one()
    return {
        "total": total,
        "confirmed": confirmed,
        "declined": declined,
        "no_answer": total - confirmed - declined,
    }


@router.get("/{account_type}/{account_id}")
async def get_account_detail(
    account_type: str, account_id: uuid.UUID,
    _admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db),
):
    """Full detail view for a single account — profile fields, spend,
    account age, SIM count (every number ever owned, not just the
    currently-active one — see number_provisioning.py's design), and
    confirm/decline/no-answer breakdowns split into two buckets:
    appointment calls (business only — event accounts have no
    appointments at all) and event/campaign calls (both account types,
    since business accounts can also run campaigns).

    Appointment vs event bucketing is done via Call.appointment_id /
    Call.campaign_id directly (NOT the campaign_id-is-null check used
    elsewhere in this codebase for a different purpose) — demo calls have
    neither set, so they're correctly excluded from both buckets rather
    than leaking into "appointment calls" the way a campaign_id-only
    check would."""
    model = _MODEL.get(account_type)
    if model is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "account_type must be business|event")
    account = await db.get(model, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    is_business = account_type == "business"

    sim_count = (
        await db.execute(
            select(func.count(PhoneNumberPool.id)).where(
                PhoneNumberPool.assigned_account_type == account_type,
                PhoneNumberPool.assigned_account_id == account.id,
            )
        )
    ).scalar_one()

    total_calls = (
        await db.execute(
            select(func.count(Call.id)).where(
                (Call.business_account_id if is_business else Call.event_account_id) == account.id,
            )
        )
    ).scalar_one()

    appointment_stats = None
    if is_business:
        appointment_stats = await _outcome_counts(
            db, [Call.business_account_id == account.id, Call.appointment_id.is_not(None)]
        )

    col = Event.business_account_id if is_business else Event.event_account_id
    event_call_where = [
        Call.campaign_id.is_not(None),
        Call.campaign_id.in_(
            select(Campaign.id).join(Event, Event.id == Campaign.event_id).where(col == account.id)
        ),
    ]
    event_stats = await _outcome_counts(db, event_call_where)

    campaign_spend = (
        await db.execute(
            select(func.coalesce(func.sum(Campaign.cost_usd), 0.0))
            .select_from(Campaign)
            .join(Event, Event.id == Campaign.event_id)
            .where(col == account.id)
        )
    ).scalar_one()
    total_spent = float(campaign_spend)
    if is_business and account.stripe_customer_id:
        total_spent += stripe_service.sum_paid_invoices(account.stripe_customer_id)
    elif not is_business and account.sim_fee_charged:
        total_spent += settings.event_sim_fee_usd

    account_age_days = (datetime.now(timezone.utc) - account.created_at).days

    return {
        "id": str(account.id),
        "account_type": account_type,
        "name": account.name,
        "email": account.email,
        "phone_number": account.phone_number,
        "country": account.country,
        "status": account.status.value,
        "manual_review_required": account.manual_review_required,
        "created_at": account.created_at.isoformat(),
        "account_age_days": account_age_days,
        "sim_count": sim_count,
        "total_calls": total_calls,
        "total_spent_usd": round(total_spent, 2),
        "appointment_stats": appointment_stats,  # None for event accounts
        "event_stats": event_stats,
    }


@router.post("/{account_type}/{account_id}/suspend")
async def suspend_account(
    account_type: str, account_id: uuid.UUID,
    _admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db),
):
    account = await db.get(_MODEL[account_type], account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    account.status = AccountStatus.suspended
    await db.commit()
    return {"status": "suspended"}


@router.post("/{account_type}/{account_id}/reinstate")
async def reinstate_account(
    account_type: str, account_id: uuid.UUID,
    _admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db),
):
    account = await db.get(_MODEL[account_type], account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    account.status = AccountStatus.active
    account.manual_review_required = False
    await db.commit()
    return {"status": "active"}