"""Public, unauthenticated fan self-signup — the shareable link an event
organizer posts on their own social platforms (per the actual marketing use
case this exists for: "sign up to get a confirmation call about this event").

Deliberately a SEPARATE router/file from app/api/public/v1/events.py, even
though both live under /api/public — that one requires an API key
(external integrators); this one must have NO auth at all, since it's
meant to be opened directly by anonymous fans clicking a link. Mixing the
two auth models into one router/file would make it too easy to accidentally
apply (or forget) an auth dependency on the wrong endpoint.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models import Event, FanContact
from app.services.bulk_upload import valid_phone

router = APIRouter(prefix="/event-signup", tags=["public:event-signup"])


class SignupRequest(BaseModel):
    name: str | None = None
    phone_number: str
    consent: bool


class EventPublicOut(BaseModel):
    name: str


@router.get("/{event_id}", response_model=EventPublicOut)
async def get_event_public(event_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Deliberately minimal — only what the signup page needs to render
    (the event name). Never exposes the owning account, campaigns, or the
    existing contact list to an anonymous visitor."""
    event = await db.get(Event, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    return EventPublicOut(name=event.name)


@router.post("/{event_id}", status_code=201)
async def signup(event_id: uuid.UUID, body: SignupRequest, db: AsyncSession = Depends(get_db)):
    event = await db.get(Event, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")

    if not body.consent:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Consent is required to sign up")

    phone = valid_phone(body.phone_number)
    if not phone:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Enter a valid phone number, e.g. +15551234567")

    # Same phone can't join the same event's list twice — the organizer's
    # CSV upload already enforces per-batch dedup; this is the equivalent
    # for self-signup, checked against the event's FULL existing list
    # (including prior CSV-uploaded rows), not just other signups.
    existing = (
        await db.execute(
            select(FanContact).where(FanContact.event_id == event_id, FanContact.phone_number == phone)
        )
    ).scalar_one_or_none()
    if existing:
        return {"already_signed_up": True}

    db.add(FanContact(
        event_id=event_id, name=body.name, phone_number=phone,
        consent_given_at=datetime.now(timezone.utc),
    ))
    await db.commit()
    return {"already_signed_up": False}