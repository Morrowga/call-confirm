"""Public v1 events API (scope: events:create). Business accounts using the
event feature via API keys — calls metered into their existing subscription."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_api_key
from app.models import (
    BusinessAccount, Campaign, CampaignState, Event, FanContact, SCOPE_EVENTS_CREATE,
)
from app.schemas.common import CampaignCreate, CampaignOut, EventCreate, EventOut
from app.services.risk import pipeline

router = APIRouter(prefix="/events", tags=["events"])


@router.post("", response_model=EventOut, status_code=201)
async def create_event(
    body: EventCreate,
    account: BusinessAccount = Depends(require_api_key(SCOPE_EVENTS_CREATE)),
    db: AsyncSession = Depends(get_db),
):
    event = Event(
        business_account_id=account.id, name=body.name, release_deadline=body.release_deadline
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event


@router.post("/campaigns", response_model=CampaignOut, status_code=201)
async def create_campaign(
    body: CampaignCreate,
    account: BusinessAccount = Depends(require_api_key(SCOPE_EVENTS_CREATE)),
    db: AsyncSession = Depends(get_db),
):
    event = await db.get(Event, body.event_id)
    if event is None or event.business_account_id != account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    campaign = Campaign(
        event_id=event.id, message_template=body.message_template, is_rush_tier=body.rush_tier
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return campaign


@router.post("/campaigns/{campaign_id}/send", response_model=CampaignOut)
async def send_campaign(
    campaign_id: uuid.UUID,
    account: BusinessAccount = Depends(require_api_key(SCOPE_EVENTS_CREATE)),
    db: AsyncSession = Depends(get_db),
):
    """Business-account send: no separate checkout — metered into the existing
    subscription. Must clear the 4-layer risk pipeline first."""
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    event = await db.get(Event, campaign.event_id)
    if event.business_account_id != account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    if campaign.state != CampaignState.draft:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Campaign is {campaign.state.value}")

    fan_count = (
        await db.execute(select(func.count(FanContact.id)).where(FanContact.event_id == event.id))
    ).scalar_one()

    decision = await pipeline.evaluate(
        db, account=account, account_type="business",
        message=campaign.message_template, kind="event",
        list_size=fan_count, campaign_id=campaign.id,
    )
    if decision.action == "reject":
        campaign.state = CampaignState.rejected
        await db.commit()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            {"blocked": decision.hard_block_reasons})
    if decision.action == "hold":
        campaign.state = CampaignState.held_for_review
        await db.commit()
        return campaign

    campaign.state = CampaignState.paid  # metered billing; no checkout gate
    await db.commit()
    from app.tasks.calling import send_campaign as send_task
    send_task.delay(str(campaign.id))
    await db.refresh(campaign)
    return campaign