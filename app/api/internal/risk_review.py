"""Platform admin: risk review queue.

Two distinct kinds of RiskScore row land here, told apart by which
foreign keys are set:

  * ACCOUNT-level (campaign_id AND appointment_id both NULL) — Layer 4's
    behavioral-flag hold (see services/risk/pipeline.py). Created when an
    account's ongoing behavior trips the composite score threshold
    mid-use, not at any single action's creation time. Clearing this
    un-flags the account (manual_review_required = False) AND resumes any
    of its campaigns that were paused mid-dispatch when the flag tripped
    (CampaignState.paused_for_review), by re-invoking send_campaign for
    each. Already-dispatched calls were never affected by the flag in the
    first place — only NEW dispatching was ever blocked.

  * CAMPAIGN-level (campaign_id set) — a hold on a campaign BEFORE it was
    ever dispatched at all (CampaignState.held_for_review), distinct from
    the paused_for_review case above. Clearing this moves the campaign to
    `paid`, letting the normal scan_due_campaigns pick it up at its
    scheduled_at (immediately, if that's already in the past by now).

Appointment-level RiskScore rows (appointment_id set) are scoring/
visibility only today — there's no distinct "held" AppointmentStatus to
resume, so clearing/rejecting one only updates its own review_status.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_platform_admin
from app.models import (
    Appointment, BusinessAccount, Campaign, CampaignState, Event, EventAccount, ReviewStatus, RiskScore,
)
from app.models.accounts import AccountStatus
from app.services import notifications

router = APIRouter(prefix="/risk-review", tags=["internal:risk-review"])


async def _account_display(db: AsyncSession, account_type: str | None, account_id: uuid.UUID | None) -> dict:
    if not account_type or not account_id:
        return {"name": None, "email": None}
    model = BusinessAccount if account_type == "business" else EventAccount
    account = await db.get(model, account_id)
    if account is None:
        return {"name": None, "email": None}
    return {"name": account.name, "email": account.email}


@router.get("/queue")
async def list_queue(_admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(RiskScore).where(RiskScore.review_status == ReviewStatus.pending)
            .order_by(RiskScore.created_at.desc())
        )
    ).scalars().all()
    result = []
    for r in rows:
        display = await _account_display(db, r.account_type, r.account_id)
        kind = "account" if r.campaign_id is None and r.appointment_id is None else (
            "campaign" if r.campaign_id is not None else "appointment"
        )

        # The actual message content — what an admin actually needs to
        # read to judge Layer 1 keyword hits / Layer 2 template deviation
        # for themselves, not just trust the score. Previously the queue
        # only ever showed raw campaign_id/appointment_id UUIDs.
        campaign_message = None
        appointment_detail = None
        if r.campaign_id is not None:
            campaign = await db.get(Campaign, r.campaign_id)
            if campaign is not None:
                campaign_message = campaign.message_template
        if r.appointment_id is not None:
            appt = await db.get(Appointment, r.appointment_id)
            if appt is not None:
                appointment_detail = {
                    "client_name": appt.client_name,
                    "confirmation_type": appt.confirmation_type.value,
                    "subject_detail": appt.subject_detail,
                }

        result.append({
            "id": str(r.id),
            "kind": kind,
            "account_type": r.account_type,
            "account_id": str(r.account_id) if r.account_id else None,
            "account_name": display["name"],
            "account_email": display["email"],
            "campaign_id": str(r.campaign_id) if r.campaign_id else None,
            "campaign_message": campaign_message,
            "appointment_id": str(r.appointment_id) if r.appointment_id else None,
            "appointment_detail": appointment_detail,
            "composite_score": r.composite_score,
            "factor_breakdown": r.factor_breakdown,
            "created_at": r.created_at,
        })
    return result


@router.post("/{risk_score_id}/clear")
async def clear_risk_score(
    risk_score_id: uuid.UUID,
    admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db),
):
    r = await db.get(RiskScore, risk_score_id)
    if r is None or r.review_status != ReviewStatus.pending:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    r.review_status = ReviewStatus.cleared
    r.reviewed_by = admin.id

    if r.campaign_id is None and r.appointment_id is None:
        if not r.account_type or not r.account_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed account-level risk score row")
        model = BusinessAccount if r.account_type == "business" else EventAccount
        account = await db.get(model, r.account_id)
        if account is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
        account.manual_review_required = False

        owner_col = Event.business_account_id if r.account_type == "business" else Event.event_account_id
        paused = (
            await db.execute(
                select(Campaign)
                .join(Event, Event.id == Campaign.event_id)
                .where(Campaign.state == CampaignState.paused_for_review, owner_col == r.account_id)
            )
        ).scalars().all()
        resumed_ids = []
        for campaign in paused:
            campaign.state = CampaignState.sending
            resumed_ids.append(str(campaign.id))
        await db.commit()

        for campaign_id in resumed_ids:
            from app.tasks.calling import send_campaign
            send_campaign.delay(campaign_id)

        if account.email:
            notifications.send_email(account.email, "account_review_cleared")

        return {"cleared": True, "account_unflagged": True, "resumed_campaigns": resumed_ids}

    if r.campaign_id is not None:
        # Best-effort: the campaign may no longer exist (deleted after
        # this review row was created — this is exactly what happened
        # with early test data). Previously this raised a 404 and blocked
        # the review from EVER being dismissed if that happened — the
        # RiskScore's own review_status is now always updated regardless;
        # only the campaign-state transition is skipped if there's
        # nothing left to transition.
        campaign = await db.get(Campaign, r.campaign_id)
        if campaign is not None and campaign.state == CampaignState.held_for_review:
            campaign.state = CampaignState.paid
        await db.commit()
        return {"cleared": True, "campaign_id": str(r.campaign_id), "campaign_found": campaign is not None}

    await db.commit()
    return {"cleared": True}


@router.post("/{risk_score_id}/reject")
async def reject_risk_score(
    risk_score_id: uuid.UUID,
    admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db),
):
    """Rejecting confirms the flagged behavior was genuinely bad — unlike
    clear, this takes a real enforcement action rather than just marking
    the row reviewed. Account-level: suspends the account outright.
    Campaign-level: marks the campaign rejected (terminal state, never
    dispatches)."""
    r = await db.get(RiskScore, risk_score_id)
    if r is None or r.review_status != ReviewStatus.pending:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    r.review_status = ReviewStatus.rejected
    r.reviewed_by = admin.id

    if r.campaign_id is None and r.appointment_id is None:
        if not r.account_type or not r.account_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed account-level risk score row")
        model = BusinessAccount if r.account_type == "business" else EventAccount
        account = await db.get(model, r.account_id)
        if account is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
        account.status = AccountStatus.suspended
        await db.commit()
        return {"rejected": True, "account_suspended": True}

    if r.campaign_id is not None:
        campaign = await db.get(Campaign, r.campaign_id)
        if campaign is not None:
            campaign.state = CampaignState.rejected
        await db.commit()
        return {"rejected": True, "campaign_id": str(r.campaign_id), "campaign_found": campaign is not None}

    await db.commit()
    return {"rejected": True}