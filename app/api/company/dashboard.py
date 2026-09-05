"""Company panel dashboard: account overview, calls, results, bulk uploads."""
import json
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import current_business_account, current_event_account, current_panel_account
from app.core.languages import VALID_LANGUAGES
from app.models import (
    Appointment, BulkUpload, BusinessAccount, Call, CallResult, Campaign, ConfirmationType, Event,
    EventAccount, FanContact, VoiceMessage,
)
from app.schemas.common import BulkCommitOut, BulkCommitRequest, CsvUploadOut, DashboardOverview
from app.services import bulk_upload as bulk
from app.services import recording_storage
from app.services import confirmation_templates as templates
from app.services import stripe_service

router = APIRouter(prefix="/dashboard", tags=["company:dashboard"])


@router.get("/overview", response_model=DashboardOverview)
async def overview(
    account=Depends(current_panel_account), db: AsyncSession = Depends(get_db)
):
    is_business = isinstance(account, BusinessAccount)
    col = Call.business_account_id if is_business else Call.event_account_id
    total_calls = (
        await db.execute(
            select(func.count(Call.id)).where(col == account.id, Call.campaign_id.is_(None))
        )
    ).scalar_one()
    # Separate count for event/campaign calls — shown as its own card on
    # the dashboard, distinct from the subscription/appointment total
    # above. Business accounts can have both; event accounts only ever
    # have this one (they have no appointments at all).
    total_event_calls = (
        await db.execute(
            select(func.count(Call.id)).where(col == account.id, Call.campaign_id.is_not(None))
        )
    ).scalar_one()

    next_billing_date = None
    if is_business and account.stripe_subscription_id:
        period_end = stripe_service.get_current_period_end(account.stripe_subscription_id)
        if period_end:
            next_billing_date = datetime.fromtimestamp(period_end, tz=timezone.utc)

    return DashboardOverview(
        account_type="business" if is_business else "event",
        status=account.status.value,
        demo_calls_used=account.demo_calls_used,
        total_calls=total_calls,
        total_event_calls=total_event_calls,
        manual_review_required=account.manual_review_required,
        preferred_language=account.preferred_language,
        subscription_tier=account.subscription_tier.value if is_business else None,
        voice_messaging_addon=account.voice_messaging_addon if is_business else None,
        free_calls_used=account.free_calls_used if is_business else None,
        per_call_rate_usd=settings.per_call_rate_usd if is_business else None,
        next_billing_date=next_billing_date,
        business_name=account.name if is_business else None,
    )


def _call_status(keypress: str | None) -> str:
    """Same vocabulary and mapping as Appointment.status (see webhooks.py's
    _store_result) — keeps the Calls page's status language identical to
    the Appointments page. A call that was technically answered but got no
    real response (recipient hung up before pressing anything) resolves to
    no_answer here too, same as the appointment-level fix — this
    intentionally does NOT distinguish "picked up, no response" from
    "never picked up at all"; both mean the same thing from a business
    outcome standpoint, so both pages should say the same thing."""
    return {
        "1": "confirmed", "2": "declined", "3": "voice_message_left", "9": "reported",
    }.get(keypress or "", "no_answer")


def _serialize_call_row(call: Call, result: CallResult | None, vm: VoiceMessage | None) -> dict:
    return {
        "id": str(call.id),
        "to": call.to_number,
        "started_at": call.started_at,
        "duration_seconds": call.duration_seconds,
        "voice_tier": call.voice_tier.value,
        "environment": call.environment.value,
        "dial_outcome": result.dial_outcome.value if result else None,
        "keypress": result.keypress.value if result else None,
        # Same vocabulary/derivation as Appointment.status (see
        # webhooks.py's twilio_gather) — confirmed/declined/
        # voice_message_left/reported/no_answer — so the panel shows one
        # consistent status language everywhere, not raw keypress digits
        # or dial_outcome enum values.
        "status": _call_status(result.keypress.value if result else None),
        "voice_message_url": recording_storage.recording_url(vm.s3_key, str(call.id)) if vm else None,
        "voice_message_duration_seconds": vm.duration_seconds if vm else None,
    }


async def _list_calls(
    db: AsyncSession, account, limit: int, offset: int, search: str | None, campaign_only: bool,
    event_id: uuid.UUID | None = None,
) -> list[dict]:
    is_business = isinstance(account, BusinessAccount)
    col = Call.business_account_id if is_business else Call.event_account_id
    where = [col == account.id]
    where.append(Call.campaign_id.is_not(None) if campaign_only else Call.campaign_id.is_(None))
    if search:
        where.append(Call.to_number.ilike(f"%{search}%"))
    query = (
        select(Call, CallResult, VoiceMessage)
        .outerjoin(CallResult, CallResult.call_id == Call.id)
        # deleted_at IS NULL — a message past its 90-day retention window
        # (soft-deleted by the retention task) simply has nothing to
        # play; excluded here rather than generating a presigned URL for
        # audio that no longer actually exists in S3.
        .outerjoin(VoiceMessage, (VoiceMessage.call_id == Call.id) & (VoiceMessage.deleted_at.is_(None)))
    )
    if event_id is not None:
        # Scope to ONE event's calls — previously /calls/events had no way
        # to filter by event at all, so the "View calls" button on the
        # Events page (whichever event's row it was clicked from) always
        # showed EVERY campaign call across the whole account instead of
        # just that one event's. Joins through Campaign since Call only
        # has campaign_id directly, not event_id.
        query = query.join(Campaign, Campaign.id == Call.campaign_id).where(Campaign.event_id == event_id)
    rows = (
        await db.execute(
            query.where(*where)                            # own data only
            .order_by(Call.created_at.desc())
            .limit(min(limit, 200)).offset(offset)
        )
    ).all()
    return [_serialize_call_row(call, result, vm) for call, result, vm in rows]


@router.get("/calls")
async def list_calls(
    account=Depends(current_panel_account), db: AsyncSession = Depends(get_db),
    limit: int = 50, offset: int = 0, search: str | None = None,
):
    """Appointment (subscription) calls only — event/campaign calls have
    their own separate list at /calls/events, so the two no longer mix on
    this page. Event accounts never have appointment calls at all, so this
    is correctly always empty for them; they should use /calls/events."""
    return await _list_calls(db, account, limit, offset, search, campaign_only=False)


@router.get("/calls/events")
async def list_event_calls(
    account=Depends(current_panel_account), db: AsyncSession = Depends(get_db),
    limit: int = 50, offset: int = 0, search: str | None = None, event_id: uuid.UUID | None = None,
):
    """Event/campaign calls only — the counterpart to /calls above. Reached
    from a button on the Events page rather than the main nav, since it's
    scoped to a specific event/campaign context rather than being a
    standalone top-level list the way appointment calls are.

    event_id is optional so this endpoint still works as "all my event
    calls" if ever needed generically, but the Events page's "View calls"
    button always passes it now — see the fix in EventCallsPage.tsx."""
    return await _list_calls(db, account, limit, offset, search, campaign_only=True, event_id=event_id)


@router.get("/demo-calls")
async def list_demo_calls(
    account=Depends(current_panel_account), db: AsyncSession = Depends(get_db),
    limit: int = 20,
):
    """This account's demo call history — shown directly under the "Try a
    demo call" card on the Dashboard. Added because event accounts no
    longer have a general Calls nav item at all (appointment calls are
    business-only, event/campaign calls have their own separate page), so
    without this there was nowhere for an event account to ever see the
    result of a demo call they placed. Demo calls have neither
    appointment_id nor campaign_id set (see call_orchestrator.
    create_demo_call), so they're identified by Call.is_demo directly
    rather than by the campaign_id null-check _list_calls uses — that
    check alone can't distinguish "appointment call" from "demo call",
    both of which have campaign_id IS NULL."""
    is_business = isinstance(account, BusinessAccount)
    col = Call.business_account_id if is_business else Call.event_account_id
    rows = (
        await db.execute(
            select(Call, CallResult, VoiceMessage)
            .outerjoin(CallResult, CallResult.call_id == Call.id)
            .outerjoin(VoiceMessage, (VoiceMessage.call_id == Call.id) & (VoiceMessage.deleted_at.is_(None)))
            .where(col == account.id, Call.is_demo.is_(True))
            .order_by(Call.created_at.desc())
            .limit(min(limit, 50))
        )
    ).all()
    return [_serialize_call_row(call, result, vm) for call, result, vm in rows]


def _period_bounds(period: str, on_date: date) -> tuple[datetime, datetime]:
    """Returns [start, end) as UTC-midnight-aligned datetimes for the given
    period containing on_date. Week is Monday-Sunday (ISO convention)."""
    if period == "day":
        start = datetime(on_date.year, on_date.month, on_date.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
    elif period == "week":
        monday = on_date - timedelta(days=on_date.weekday())
        start = datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
        end = start + timedelta(days=7)
    elif period == "month":
        start = datetime(on_date.year, on_date.month, 1, tzinfo=timezone.utc)
        end = datetime(on_date.year + 1, 1, 1, tzinfo=timezone.utc) if on_date.month == 12 \
            else datetime(on_date.year, on_date.month + 1, 1, tzinfo=timezone.utc)
    else:  # year
        start = datetime(on_date.year, 1, 1, tzinfo=timezone.utc)
        end = datetime(on_date.year + 1, 1, 1, tzinfo=timezone.utc)
    return start, end


async def _compute_stats(
    db: AsyncSession, account, period: str, target_date: date, campaign_only: bool,
) -> dict:
    start, end = _period_bounds(period, target_date)
    is_business = isinstance(account, BusinessAccount)
    col = Call.business_account_id if is_business else Call.event_account_id
    where = [col == account.id, Call.created_at >= start, Call.created_at < end]
    where.append(Call.campaign_id.is_not(None) if campaign_only else Call.campaign_id.is_(None))
    rows = (
        await db.execute(
            select(CallResult.keypress)
            .join(Call, Call.id == CallResult.call_id)
            .where(*where)
        )
    ).all()

    counts = {"confirmed": 0, "declined": 0, "no_answer": 0, "voice_message_left": 0, "reported": 0}
    for (keypress,) in rows:
        s = _call_status(keypress.value if keypress else None)
        if s in counts:
            counts[s] += 1

    total = len(rows)
    rate = lambda n: round((n / total) * 100, 1) if total else 0.0  # noqa: E731
    return {
        "period": period,
        "period_start": start,
        "period_end": end,
        "total": total,
        "confirmed": counts["confirmed"],
        "declined": counts["declined"],
        "no_answer": counts["no_answer"],
        "voice_message_left": counts["voice_message_left"],
        "reported": counts["reported"],
        "confirm_rate": rate(counts["confirmed"]),
        "decline_rate": rate(counts["declined"]),
        "no_answer_rate": rate(counts["no_answer"]),
        "voice_message_rate": rate(counts["voice_message_left"]),
        "report_rate": rate(counts["reported"]),
    }


@router.get("/stats")
async def call_stats(
    account=Depends(current_panel_account), db: AsyncSession = Depends(get_db),
    period: str = "day", on_date: str | None = None,
):
    """Confirm/decline/no-answer/voice-message/report rates over a period —
    reuses the exact same status vocabulary as the Calls/Appointments pages
    (_call_status) so the numbers here are always consistent with what's
    shown everywhere else. Appointment (subscription) calls only — see
    /stats/events for the event/campaign-call counterpart."""
    if period not in ("day", "week", "month", "year"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "period must be day, week, month, or year")
    today = datetime.now(timezone.utc).date()
    target_date = date.fromisoformat(on_date) if on_date else today
    if target_date > today:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Date cannot be in the future")
    return await _compute_stats(db, account, period, target_date, campaign_only=False)


@router.get("/stats/events")
async def event_call_stats(
    account=Depends(current_panel_account), db: AsyncSession = Depends(get_db),
    period: str = "day", on_date: str | None = None,
):
    """Same shape and vocabulary as /stats above, scoped to event/campaign
    calls only — the data source for the Dashboard's "Event calls" tab."""
    if period not in ("day", "week", "month", "year"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "period must be day, week, month, or year")
    today = datetime.now(timezone.utc).date()
    target_date = date.fromisoformat(on_date) if on_date else today
    if target_date > today:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Date cannot be in the future")
    return await _compute_stats(db, account, period, target_date, campaign_only=True)


@router.get("/calls/{call_id}/recording")
async def get_call_recording(
    call_id: uuid.UUID,
    account=Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    """Local/test-only serving path — production recordings are fetched via
    the presigned S3 URL returned directly in /calls, never through this
    endpoint. Local disk has no built-in access control equivalent to an S3
    presigned URL's signature, so this re-verifies ownership the same way
    every other 'own data only' endpoint in this codebase does, before
    reading the file."""
    is_business = isinstance(account, BusinessAccount)
    col = Call.business_account_id if is_business else Call.event_account_id
    call = await db.get(Call, call_id)
    if call is None or getattr(call, col.key) != account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recording not found")
    vm = (
        await db.execute(select(VoiceMessage).where(VoiceMessage.call_id == call_id))
    ).scalar_one_or_none()
    if vm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recording not found")
    audio = recording_storage.read_local_recording(vm.s3_key)
    if audio is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recording not found")
    return Response(content=audio, media_type="audio/mpeg")


@router.get("/uploads/appointments/sample")
async def sample_appointments_csv(account: BusinessAccount = Depends(current_business_account)):
    """The exact required CSV format, with one example row per broad case
    (a type with a real scheduled time vs. one that's send-time-only)."""
    content = bulk.sample_appointments_csv()
    return Response(
        content=content, media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sample_appointments.csv"},
    )


@router.post("/uploads/appointments", response_model=CsvUploadOut)
async def preview_appointments(
    file: UploadFile = File(...),
    timezone_name: str = Form(..., alias="timezone"),
    account: BusinessAccount = Depends(current_business_account),
):
    """Strict fixed-column format (no AI column mapping) — all-or-nothing:
    if any row has a problem, nothing is returned as valid; the shop fixes
    the file and re-uploads, rather than reviewing a partially-broken batch.
    `timezone` is the one global setting for the whole batch; `language` is
    a per-row CSV column, since a business can genuinely have customers who
    speak different languages. Writes nothing to the database yet — that
    only happens via /uploads/appointments/commit, after the shop reviews
    this result."""
    content = await file.read()
    available_types = [t.value for t in templates.available_types(account.industry)]
    result = bulk.validate_appointments_csv(file.filename, content, timezone_name, available_types, VALID_LANGUAGES)
    return CsvUploadOut(success=result.success, errors=result.errors[:100], rows=result.rows)


@router.post("/uploads/appointments/commit", response_model=BulkCommitOut)
async def commit_appointments(
    body: BulkCommitRequest,
    account: BusinessAccount = Depends(current_business_account),
    db: AsyncSession = Depends(get_db),
):
    """Actually creates appointments from previewed (and possibly
    owner-edited) rows. Called with one row for a single "Add" action, or
    the full list for "Add all"."""
    for row in body.rows:
        data = row.model_dump()
        data["confirmation_type"] = ConfirmationType(data["confirmation_type"])
        proper_nouns = data.pop("subject_detail_proper_nouns", None)
        data["subject_detail_proper_nouns"] = json.dumps(proper_nouns) if proper_nouns else None
        db.add(Appointment(business_account_id=account.id, **data))
    db.add(BulkUpload(
        account_type="business", account_id=account.id, kind="appointments",
        s3_key="", total_rows=len(body.rows), valid_rows=len(body.rows),
        duplicate_rows=0, invalid_rows=0, column_mapping={}, consent_attested=True,
    ))
    await db.commit()
    return BulkCommitOut(created=len(body.rows))


@router.get("/uploads/fans/sample")
async def sample_fans_csv(account=Depends(current_panel_account)):
    return Response(
        content=bulk.sample_fans_csv(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sample_contacts.csv"},
    )


@router.post("/uploads/fans/{event_id}")
async def upload_fans(
    event_id: uuid.UUID,
    consent_attested: bool = False,
    file: UploadFile = File(...),
    account=Depends(current_panel_account),
    db: AsyncSession = Depends(get_db),
):
    if not consent_attested:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "You must confirm everyone on this list has agreed to receive event calls from you",
        )
    event = await db.get(Event, event_id)
    is_business = isinstance(account, BusinessAccount)
    owner_id = event.business_account_id if is_business else event.event_account_id if event else None
    if event is None or owner_id != account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    content = await file.read()
    # Strict fixed-column format (name, phone number) — same discipline as
    # the appointments CSV flow, replacing the older flexible AI-column-
    # mapping approach, which didn't validate against a known format at all.
    # Existing phones (from a prior upload or public signups) are checked
    # too, so re-uploading or an overlapping list doesn't create duplicates.
    existing_phones = set(
        (await db.execute(select(FanContact.phone_number).where(FanContact.event_id == event.id)))
        .scalars().all()
    )
    result = bulk.validate_fans_csv(file.filename, content, existing_phones)
    if not result.success:
        return {"summary": "", "errors": result.errors, "rows": []}
    for row in result.rows:
        db.add(FanContact(event_id=event.id, **row))
    db.add(BulkUpload(
        account_type="business" if is_business else "event", account_id=account.id,
        kind="fans", s3_key="", total_rows=len(result.rows) + result.duplicates, valid_rows=len(result.rows),
        duplicate_rows=result.duplicates, invalid_rows=0, column_mapping={}, consent_attested=True,
    ))
    await db.commit()
    summary = f"{len(result.rows)} contact(s) added."
    if result.duplicates:
        summary += f" {result.duplicates} duplicate(s) skipped."
    return {
        "summary": summary,
        "errors": [],
        "rows": result.rows,
    }