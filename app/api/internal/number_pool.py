"""Platform admin: number pool dashboard + auto-purchase cap + approval queue."""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import require_platform_admin
from app.models import (
    BusinessAccount, EventAccount, NumberStatus, PhoneNumberPool, PlatformConfig,
    ProvisioningRequest,
)
from app.services import notifications, number_provisioning

router = APIRouter(prefix="/number-pool", tags=["internal:numbers"])


@router.get("/dashboard")
async def dashboard(_admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db)):
    by_country = (
        await db.execute(
            select(PhoneNumberPool.country, PhoneNumberPool.status, func.count(PhoneNumberPool.id))
            .group_by(PhoneNumberPool.country, PhoneNumberPool.status)
        )
    ).all()
    stats: dict[str, dict] = {}
    for country, st, count in by_country:
        stats.setdefault(country, {"assigned": 0, "available": 0, "demo": 0, "total": 0})
        stats[country][st.value] += count
        stats[country]["total"] += count

    # Purchase rate over time (last 30 days, daily buckets).
    since = datetime.now(timezone.utc) - timedelta(days=30)
    # Raw SQL here, not the ORM expression — reusing the same
    # func.date_trunc(...) Python object across select/group_by/order_by
    # still produced three SEPARATE bound parameters for the "day" literal
    # (visible as $1/$3/$4 in the failing query), which Postgres can't
    # recognize as equivalent for GROUP BY matching purposes. Writing
    # 'day' directly in the SQL text sidesteps the whole issue — it's
    # never parameterized at all, so there's nothing for Postgres to fail
    # to match. GROUP BY 1 / ORDER BY 1 reference the SELECT's first
    # column by position, avoiding any expression-identity question too.
    rate = (
        await db.execute(
            text(
                """
                SELECT date_trunc('day', purchased_at) AS day, count(*) AS count
                FROM phone_number_pool
                WHERE purchased_at >= :since
                GROUP BY 1
                ORDER BY 1
                """
            ),
            {"since": since},
        )
    ).all()
    return {
        "by_country": stats,
        "purchase_rate_daily": [{"day": d.date().isoformat(), "purchased": c} for d, c in rate],
    }


@router.get("/auto-purchase-caps")
async def get_caps(_admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db)):
    """Flat {country: cap} dict — no global cap concept. Was missing
    entirely as a GET before — only PUT existed, so the admin caps editor
    had no way to load what was already saved and always started blank
    on every page visit, even after caps had genuinely been saved."""
    row = await db.get(PlatformConfig, "number_auto_purchase_caps")
    if row and row.value:
        return row.value
    return {}


@router.put("/auto-purchase-caps")
async def set_caps(
    caps: dict[str, int],
    _admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db),
):
    """Body: {"US": 200, "GB": 50, "MM": 100, ...} — flat, no global key."""
    row = await db.get(PlatformConfig, "number_auto_purchase_caps")
    if row:
        row.value = caps
    else:
        db.add(PlatformConfig(key="number_auto_purchase_caps", value=caps))
    await db.commit()
    return {"saved": caps}


async def _account_display(db: AsyncSession, account_type: str, account_id: uuid.UUID) -> dict:
    """Name + email for an approval-queue row — previously the queue only
    ever returned the raw account_id, which is what was showing up as a
    long unreadable UUID string in the admin UI instead of who the
    request is actually for."""
    model = BusinessAccount if account_type == "business" else EventAccount
    account = await db.get(model, account_id)
    if account is None:
        return {"name": None, "email": None}
    return {"name": account.name, "email": account.email}


@router.get("/approval-queue")
async def approval_queue(_admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(ProvisioningRequest).where(ProvisioningRequest.status == "pending")
        )
    ).scalars().all()
    result = []
    for r in rows:
        display = await _account_display(db, r.account_type, r.account_id)
        result.append({
            "id": str(r.id), "account_type": r.account_type, "account_id": str(r.account_id),
            "account_name": display["name"], "account_email": display["email"],
            "country": r.country, "created_at": r.created_at, "is_additional": r.is_additional,
        })
    return result


async def _account_email(db: AsyncSession, account_type: str, account_id: uuid.UUID) -> str | None:
    model = BusinessAccount if account_type == "business" else EventAccount
    account = await db.get(model, account_id)
    return account.email if account else None


@router.post("/approval-queue/{request_id}/approve")
async def approve_provisioning(
    request_id: uuid.UUID,
    _admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db),
):
    """Purchases a real number for this request and links it — but does
    NOT hand it to the account for free anymore. Two distinct cases,
    handled the same way here:

      1. A brand-new account's FIRST number, routed here only because the
         auto-purchase cap was hit (see number_provisioning.py) — still
         free once fulfilled, matching the original design; approving
         simply completes what would have auto-purchased.
      2. A self-service "request an additional SIM" (see billing.py's
         /numbers/request) — this one is NOT free; the account holder
         must pay the one-time fee (settings.additional_sim_fee_usd)
         before it activates.

    Both cases purchase the same way and land at status="ready_for_payment"
    with the new number linked via provisioning_request_id, is_active=False
    — the number only actually goes live once the account holder confirms
    it (immediately, no charge, for case 1's flow — see billing.py's
    /numbers/pending-request/{id}/pay, which charges $0 by skipping the
    Stripe call entirely when this was the free first-number case).
    An email is always sent linking back to Settings either way."""
    req = await db.get(ProvisioningRequest, request_id)
    if req is None or req.status != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    from app.services import twilio_service
    try:
        number, sid = twilio_service.purchase_number(req.country)
    except RuntimeError as e:
        # Left at status="pending", nothing committed — safe to just click
        # Approve again later (e.g. once Twilio has inventory, or once
        # this country's regulatory paperwork with Twilio is sorted out).
        # A raw unhandled exception here previously surfaced as an opaque
        # 500 with no explanation of what actually went wrong.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Twilio purchase failed for {req.country}: {e}. The request is still pending — try Approve again later.",
        )
    number_row = PhoneNumberPool(
        number=number, country=req.country, twilio_sid=sid,
        status=NumberStatus.assigned,
        assigned_account_type=req.account_type, assigned_account_id=req.account_id,
        assigned_at=datetime.now(timezone.utc),
        is_active=False,
        provisioning_request_id=req.id,
    )
    db.add(number_row)
    req.status = "ready_for_payment"
    await db.commit()

    email = await _account_email(db, req.account_type, req.account_id)
    if email:
        notifications.send_email(
            email, "sim_ready_for_payment",
            link=f"{settings.api_base_url}/panel/settings",
        )
    return {"approved": True, "number": number}


@router.post("/approval-queue/{request_id}/reject")
async def reject_provisioning(
    request_id: uuid.UUID,
    _admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db),
):
    req = await db.get(ProvisioningRequest, request_id)
    if req is None or req.status != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    req.status = "rejected"
    req.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    return {"rejected": True}