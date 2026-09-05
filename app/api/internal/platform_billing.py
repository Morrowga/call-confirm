"""Platform admin: platform-wide billing/revenue visibility."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import require_platform_admin
from app.models import BusinessAccount, Call, Campaign, EventAccount
from app.models.accounts import AccountStatus

router = APIRouter(prefix="/billing", tags=["internal:billing"])


@router.get("/overview")
async def billing_overview(
    _admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db),
    date_from: str | None = None, date_to: str | None = None,
):
    """Platform-wide revenue/usage summary. date_from/date_to ("YYYY-MM-DD",
    both optional) filter Call.created_at — omit both for all-time
    ("Overall" section on the admin billing page); pass today's date for
    both to get "Today" (the frontend's default for that section).

    Profit is an ESTIMATE (see settings.estimated_cost_per_call_usd) —
    there's no real per-call infrastructure cost ledger. Cost applies to
    EVERY call placed in range regardless of billable status, since a
    free-50 or event/campaign call still incurs a real Twilio charge to
    us even though it isn't metered-billed to the customer individually;
    revenue only counts what was actually billable/invoiced.
    """
    parsed_from = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc) if date_from else None
    parsed_to = (
        datetime.fromisoformat(date_to).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        if date_to else None
    )
    call_range = []
    if parsed_from:
        call_range.append(Call.created_at >= parsed_from)
    if parsed_to:
        call_range.append(Call.created_at <= parsed_to)
    campaign_range = []
    if parsed_from:
        campaign_range.append(Campaign.created_at >= parsed_from)
    if parsed_to:
        campaign_range.append(Campaign.created_at <= parsed_to)

    active_business = (
        await db.execute(select(func.count(BusinessAccount.id))
                         .where(BusinessAccount.status == AccountStatus.active))
    ).scalar_one()
    active_event = (
        await db.execute(select(func.count(EventAccount.id))
                         .where(EventAccount.status == AccountStatus.active))
    ).scalar_one()
    billable_calls = (
        await db.execute(select(func.count(Call.id)).where(Call.billable.is_(True), *call_range))
    ).scalar_one()
    metered_revenue = (
        await db.execute(select(func.coalesce(func.sum(Call.cost_usd), 0.0))
                         .where(Call.billable.is_(True), *call_range))
    ).scalar_one()
    campaign_revenue = (
        await db.execute(select(func.coalesce(func.sum(Campaign.cost_usd), 0.0)).where(*campaign_range))
    ).scalar_one()
    event_call_count = (
        await db.execute(select(func.count(Call.id)).where(Call.is_event_call.is_(True), *call_range))
    ).scalar_one()

    # ALL calls placed in range, billable or not — the real cost basis.
    total_calls_all = (
        await db.execute(select(func.count(Call.id)).where(*call_range))
    ).scalar_one()

    total_revenue = round(float(metered_revenue), 2) + round(float(campaign_revenue), 2)
    estimated_cost = round(total_calls_all * settings.estimated_cost_per_call_usd, 2)
    estimated_profit = round(total_revenue - estimated_cost, 2)

    return {
        "active_business_accounts": active_business,
        "active_event_accounts": active_event,
        "billable_calls_total": billable_calls,
        "metered_call_revenue_usd": round(float(metered_revenue), 2),
        "campaign_revenue_usd": round(float(campaign_revenue), 2),
        "event_feature_calls": event_call_count,  # tagged separately for reporting
        "total_calls_all": total_calls_all,
        "total_revenue_usd": total_revenue,
        "estimated_cost_usd": estimated_cost,
        "estimated_profit_usd": estimated_profit,
    }