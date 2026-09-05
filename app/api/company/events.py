"""Company panel event/campaign endpoints — JWT-authenticated, for the panel
UI. Mirrors app/api/public/v1/events.py (API-key only, for business accounts
integrating programmatically), but this is what an Event account (or a
business account using the panel UI directly, rather than the API) actually
needs — that JWT-only path previously had NO way to create an event or
campaign at all; only upload_fans (dashboard.py) existed, and that requires
an event to already exist.

Both account types share this router (Event has both business_account_id
and event_account_id, exactly one set per row) — "own data only" is enforced
by checking whichever FK applies to the authenticated account type.

Sending/paying for a campaign lives in billing.py's campaign_checkout — used
identically by both account types (see that file's docstring). There is no
separate metered-billing "send" path for business accounts anymore, and no
cancel-after-paid path: once a campaign is paid, it's a done, non-refundable
transaction.
"""
import csv
import io
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import current_panel_account
from app.models import (
    BusinessAccount, Call, CallResult, Campaign, CampaignState, Event, EventAccount, FanContact,
    KeypressResult,
)
from app.schemas.common import (
    CampaignCreate, CampaignOut, CampaignUpdate, EventCreate, EventOut, FanContactOut, FanContactUpdate,
    PaginatedFanContacts,
)
from app.services.bulk_upload import valid_phone

router = APIRouter(prefix="/events", tags=["company:events"])


@router.post("", response_model=EventOut, status_code=201)
async def create_event(
    body: EventCreate,
    account=Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    is_business = isinstance(account, BusinessAccount)
    event = Event(
        business_account_id=account.id if is_business else None,
        event_account_id=None if is_business else account.id,
        name=body.name,
        release_deadline=body.release_deadline,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event


@router.get("")
async def list_events(
    account=Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
    search: str | None = None,
):
    is_business = isinstance(account, BusinessAccount)
    col = Event.business_account_id if is_business else Event.event_account_id
    where = [col == account.id]
    if search:
        where.append(Event.name.ilike(f"%{search}%"))
    events = (
        await db.execute(select(Event).where(*where).order_by(Event.created_at.desc()))
    ).scalars().all()
    fan_counts = {
        row.event_id: row.count
        for row in (
            await db.execute(
                select(FanContact.event_id, func.count(FanContact.id).label("count"))
                .where(FanContact.event_id.in_([e.id for e in events]))
                .group_by(FanContact.event_id)
            )
        ).all()
    }
    return [
        {
            "id": str(e.id), "name": e.name, "release_deadline": e.release_deadline,
            "fan_count": fan_counts.get(e.id, 0),
        }
        for e in events
    ]


async def _get_owned_event(db: AsyncSession, account, event_id: uuid.UUID) -> Event:
    event = await db.get(Event, event_id)
    is_business = isinstance(account, BusinessAccount)
    owner_id = event.business_account_id if is_business else event.event_account_id if event else None
    if event is None or owner_id != account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    return event


@router.get("/{event_id}/campaigns", response_model=list[CampaignOut])
async def list_campaigns(
    event_id: uuid.UUID,
    account=Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    await _get_owned_event(db, account, event_id)
    campaigns = (
        await db.execute(select(Campaign).where(Campaign.event_id == event_id).order_by(Campaign.created_at.desc()))
    ).scalars().all()
    return campaigns


def _contact_out(fan: FanContact) -> FanContactOut:
    # "source" is derived, not a stored column — a row created via public
    # self-signup always has consent_given_at set (see event_signup.py);
    # one created via CSV upload never does (consent is instead attested by
    # the uploading organizer at the batch level). No extra column needed.
    return FanContactOut(
        id=fan.id, name=fan.name, phone_number=fan.phone_number, created_at=fan.created_at,
        source="public_signup" if fan.consent_given_at else "csv_upload",
    )


@router.get("/{event_id}/contacts", response_model=PaginatedFanContacts)
async def list_contacts(
    event_id: uuid.UUID,
    account=Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
    limit: int = 20,
    offset: int = 0,
    search: str | None = None,
):
    await _get_owned_event(db, account, event_id)
    limit = min(limit, 100)
    where = [FanContact.event_id == event_id]
    if search:
        like = f"%{search}%"
        where.append((FanContact.name.ilike(like)) | (FanContact.phone_number.ilike(like)))
    total = (await db.execute(select(func.count(FanContact.id)).where(*where))).scalar_one()
    rows = (
        await db.execute(
            select(FanContact).where(*where).order_by(FanContact.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    return PaginatedFanContacts(
        items=[_contact_out(r) for r in rows], total=total, limit=limit, offset=offset,
    )


async def _get_owned_contact(db: AsyncSession, account, contact_id: uuid.UUID) -> FanContact:
    contact = await db.get(FanContact, contact_id)
    if contact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    event = await db.get(Event, contact.event_id)
    is_business = isinstance(account, BusinessAccount)
    owner_id = event.business_account_id if is_business else event.event_account_id
    if owner_id != account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    return contact


@router.patch("/contacts/{contact_id}", response_model=FanContactOut)
async def update_contact(
    contact_id: uuid.UUID,
    body: FanContactUpdate,
    account=Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    contact = await _get_owned_contact(db, account, contact_id)
    phone = valid_phone(body.phone_number)
    if not phone:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Enter a valid phone number, e.g. +15551234567")
    # Duplicate check against every OTHER contact in this same event — same
    # rule the CSV upload and public signup both already enforce, kept
    # consistent here so an edit can't silently create a duplicate either.
    existing = (
        await db.execute(
            select(FanContact).where(
                FanContact.event_id == contact.event_id, FanContact.phone_number == phone,
                FanContact.id != contact_id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Another contact in this event already has that phone number")
    contact.name = body.name
    contact.phone_number = phone
    await db.commit()
    await db.refresh(contact)
    return _contact_out(contact)


@router.delete("/contacts/{contact_id}", status_code=204)
async def delete_contact(
    contact_id: uuid.UUID,
    account=Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    contact = await _get_owned_contact(db, account, contact_id)
    await db.delete(contact)
    await db.commit()


@router.post("/campaigns", response_model=CampaignOut, status_code=201)
async def create_campaign(
    body: CampaignCreate,
    account=Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    await _get_owned_event(db, account, body.event_id)
    campaign = Campaign(
        event_id=body.event_id, title=body.title,
        message_template=body.message_template, is_rush_tier=body.rush_tier,
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return campaign


async def _get_owned_campaign(db: AsyncSession, account, campaign_id: uuid.UUID) -> Campaign:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    event = await db.get(Event, campaign.event_id)
    is_business = isinstance(account, BusinessAccount)
    owner_id = event.business_account_id if is_business else event.event_account_id
    if owner_id != account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    return campaign


@router.get("/campaigns/{campaign_id}/export")
async def export_campaign_calls(
    campaign_id: uuid.UUID,
    kind: str,  # "confirmed" | "declined" | "total"
    account=Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    """CSV export of who was actually called for this campaign, filtered by
    outcome — name (looked up from the event's contact list by phone, since
    Call has no direct FK to FanContact), phone, and when the call
    happened. `total` is every call regardless of outcome; confirmed/
    declined filter to that specific keypress."""
    campaign = await _get_owned_campaign(db, account, campaign_id)
    if kind not in ("confirmed", "declined", "no_answer", "total"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "kind must be confirmed, declined, no_answer, or total")

    query = (
        select(Call.to_number, Call.started_at)
        .join(CallResult, CallResult.call_id == Call.id, isouter=True)
        .where(Call.campaign_id == campaign_id)
    )
    if kind == "confirmed":
        query = query.where(CallResult.keypress == KeypressResult.confirmed)
    elif kind == "declined":
        query = query.where(CallResult.keypress == KeypressResult.declined)
    elif kind == "no_answer":
        query = query.where(CallResult.keypress == KeypressResult.none)
    rows = (await db.execute(query)).all()

    names = {
        row.phone_number: row.name
        for row in (
            await db.execute(
                select(FanContact.phone_number, FanContact.name).where(FanContact.event_id == campaign.event_id)
            )
        ).all()
    }

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["name", "phone number", "called at"])
    for to_number, started_at in rows:
        writer.writerow([names.get(to_number) or "", to_number, started_at.isoformat() if started_at else ""])

    return Response(
        content=buf.getvalue().encode("utf-8"), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={kind}_{campaign_id}.csv"},
    )


@router.patch("/campaigns/{campaign_id}", response_model=CampaignOut)
async def update_campaign(
    campaign_id: uuid.UUID,
    body: CampaignUpdate,
    account=Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    """Draft-only, same rule as delete — a campaign that's already been
    paid for or sent has real history behind its current title/message and
    must not be silently rewritten after the fact."""
    campaign = await _get_owned_campaign(db, account, campaign_id)
    if campaign.state != CampaignState.draft:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Campaign is {campaign.state.value} and can't be edited — only drafts can be changed.",
        )
    campaign.title = body.title
    campaign.message_template = body.message_template
    campaign.is_rush_tier = body.rush_tier
    await db.commit()
    await db.refresh(campaign)
    return campaign


@router.delete("/campaigns/{campaign_id}", status_code=204)
async def delete_campaign(
    campaign_id: uuid.UUID,
    account=Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    """Draft/rejected campaigns can be deleted because nothing was ever
    paid or dispatched. Cancelled campaigns can also be deleted — a
    cancelled campaign never actually dispatched any calls (cancel only
    ever worked in the window before dispatch), so there's no real call
    history attached to it, just a payment record; keeping it forces
    clutter with no value. Anything that's actually been paid and
    dispatched (payment_pending/paid/sending/completed/held_for_review)
    still can't be removed."""
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    event = await db.get(Event, campaign.event_id)
    is_business = isinstance(account, BusinessAccount)
    owner_id = event.business_account_id if is_business else event.event_account_id
    if owner_id != account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    if campaign.state not in (CampaignState.draft, CampaignState.rejected, CampaignState.cancelled):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Campaign is {campaign.state.value} and can't be deleted — it already has real call/billing history.",
        )
    await db.delete(campaign)
    await db.commit()


@router.delete("/{event_id}", status_code=204)
async def delete_event(
    event_id: uuid.UUID,
    account=Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    """Blocks deletion if ANY campaign under this event has moved past
    draft/rejected/cancelled — same reasoning as delete_campaign above.
    Only when every campaign is still draft, rejected, or cancelled (none
    of which ever dispatched real calls) does this cascade-delete the
    campaigns and fan contacts along with the event itself; there's no
    DB-level cascade configured on these FKs, so it's done explicitly
    here, in the safe order (children first)."""
    event = await _get_owned_event(db, account, event_id)
    campaigns = (
        await db.execute(select(Campaign).where(Campaign.event_id == event_id))
    ).scalars().all()
    safe_states = (CampaignState.draft, CampaignState.rejected, CampaignState.cancelled)
    if any(c.state not in safe_states for c in campaigns):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This event has a campaign that's already been paid or sent — it can't be deleted.",
        )
    for campaign in campaigns:
        await db.delete(campaign)
    fans = (await db.execute(select(FanContact).where(FanContact.event_id == event_id))).scalars().all()
    for fan in fans:
        await db.delete(fan)
    await db.delete(event)
    await db.commit()