"""Company panel appointments — JWT-authenticated (current_business_account),
for the account owner's own dashboard view.

Deliberately a separate module from app/api/public/v1/appointments.py rather
than reusing it directly: that router authenticates via API key + scopes
(external integrators), while this one authenticates via the panel JWT
session. Keeping them separate preserves the clean auth-method separation
that is the whole point of the public/internal/company split — a panel user
should never need an API key just to see their own appointments in the UI.
"""
import csv
import io
import json
import uuid
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import current_business_account
from app.models import Appointment, AppointmentStatus, BusinessAccount, Call, ConfirmationType, VoiceMessage
from app.schemas.common import (
    AppointmentCancelOut, AppointmentCreate, AppointmentOut, PaginatedAppointments,
    SentenceCheckOut, SentenceCheckRequest,
)
from app.services import confirmation_templates as templates
from app.services import recording_storage
from app.services import sentence_check
from app.services.risk import content as risk_content

router = APIRouter(prefix="/appointments", tags=["company:appointments"])


@router.post("/check-sentence", response_model=SentenceCheckOut)
async def check_sentence(
    body: SentenceCheckRequest,
    account: BusinessAccount = Depends(current_business_account),
    db: AsyncSession = Depends(get_db), 
):
    """Run before actual appointment creation — never creates anything.
    One AI call: detects whether subject_detail's structural language matches
    the selected language (proper nouns ignored), and returns naturalized/
    grammar-corrected variants for whichever language(s) are relevant. Falls
    back to trusting the input unchanged if AI is unavailable — this check
    is a helpful confirmation step, never a blocker."""
    try:
        conf_type = ConfirmationType(body.confirmation_type)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown confirmation_type: {body.confirmation_type}")
    # Must match exactly what the actual call announces (see webhooks.py's
    # twilio_answer) — previously this formatted body.scheduled_at (UTC)
    # directly with no timezone conversion at all, so the preview shown here
    # didn't match reality whenever the appointment's timezone wasn't UTC,
    # and embedded raw English date text in non-English previews.
    if body.scheduled_at and body.timezone:
        local_time = body.scheduled_at.astimezone(ZoneInfo(body.timezone))
        time_str = templates.format_scheduled_time(local_time, body.language)
    else:
        time_str = ""
    return await sentence_check.check_sentence(db, body, account.name, time_str)


@router.post("", response_model=AppointmentOut, status_code=201)
async def create_appointment(
    body: AppointmentCreate,
    account: BusinessAccount = Depends(current_business_account),
    db: AsyncSession = Depends(get_db),
):
    try:
        conf_type = ConfirmationType(body.confirmation_type)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown confirmation_type: {body.confirmation_type}")
    if conf_type not in templates.available_types(account.industry):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"'{conf_type.value}' is not available for the {account.industry.value} industry",
        )
    # Layer 4 behavioral restriction (see pipeline.py's
    # run_feedback_loop_for_account) — only blocks NEW creation. Anything
    # already scheduled keeps dispatching normally via scan_due_appointments;
    # this does not touch or cancel existing rows.
    if account.manual_review_required:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your account is under review — new appointments can't be created until it's cleared. "
            "Appointments already scheduled are not affected.",
        )
    if templates.needs_subject_detail(conf_type) and not (body.subject_detail or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "subject_detail is required for this confirmation type")
    if body.subject_detail:
        blocked, reasons = risk_content.check_hard_blocks(body.subject_detail)
        if blocked:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"subject_detail rejected: {'; '.join(reasons)}")

    # Types with no real future event (currently just `order`) treat
    # scheduled_at as the direct call-send time, not "an event minus an
    # offset" — force the offset to 0 regardless of what the client sent.
    offset = body.reminder_offset_minutes if templates.has_scheduled_time(conf_type) else 0

    appt = Appointment(
        business_account_id=account.id,
        client_name=body.client_name,
        phone_number=body.phone_number,
        scheduled_at=body.scheduled_at,
        timezone=body.timezone,
        language=body.language,
        reminder_offset_minutes=offset,
        confirmation_type=conf_type,
        subject_detail=body.subject_detail,
        subject_detail_proper_nouns=(
            json.dumps([pn.model_dump() for pn in body.subject_detail_proper_nouns])
            if body.subject_detail_proper_nouns else None
        ),
    )
    db.add(appt)
    await db.commit()
    await db.refresh(appt)
    return appt


@router.get("", response_model=PaginatedAppointments)
async def list_appointments(
    account: BusinessAccount = Depends(current_business_account),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
    search: str | None = None,
    sort_order: str = "asc",
):
    limit = min(limit, 200)
    base_where = [Appointment.business_account_id == account.id]
    if search:
        like = f"%{search}%"
        base_where.append(
            (Appointment.client_name.ilike(like)) | (Appointment.phone_number.ilike(like))
        )

    total = (
        await db.execute(select(func.count(Appointment.id)).where(*base_where))
    ).scalar_one()

    # scheduled_at is the one column both "scheduled for" (a real event
    # time) and "schedule to send" (a direct call-send time, for types like
    # order) actually live in — see confirmation_templates.has_scheduled_time
    # for which meaning applies per row. Sorting by it covers both of the
    # frontend's two displayed columns, since a row is only ever one or the
    # other, never both.
    order = Appointment.scheduled_at.desc() if sort_order == "desc" else Appointment.scheduled_at.asc()
    # Voice messages are linked to Call, not Appointment directly — join
    # through the call to find one, if any (DISTINCT ON picks the most
    # recent, same reasoning as the duration join used previously: normally
    # just one reminder call per appointment, but stays correct either way).
    latest_vm = (
        select(Call.appointment_id, VoiceMessage.s3_key, VoiceMessage.duration_seconds, Call.id.label("call_id"))
        .join(VoiceMessage, VoiceMessage.call_id == Call.id)
        .where(VoiceMessage.deleted_at.is_(None))
        .distinct(Call.appointment_id)
        .order_by(Call.appointment_id, Call.started_at.desc().nullslast())
        .subquery()
    )
    rows = (
        await db.execute(
            select(Appointment, latest_vm.c.s3_key, latest_vm.c.duration_seconds, latest_vm.c.call_id)
            .outerjoin(latest_vm, latest_vm.c.appointment_id == Appointment.id)
            .where(*base_where)
            .order_by(order)
            .limit(limit).offset(offset)
        )
    ).all()
    items = [
        AppointmentOut.model_validate(appt).model_copy(update={
            "voice_message_url": recording_storage.recording_url(s3_key, str(call_id)) if s3_key else None,
            "voice_message_duration_seconds": duration,
        })
        for appt, s3_key, duration, call_id in rows
    ]
    return PaginatedAppointments(items=items, total=total, limit=limit, offset=offset)


@router.get("/export")
async def export_appointments_csv(
    account: BusinessAccount = Depends(current_business_account),
    db: AsyncSession = Depends(get_db),
    search: str | None = None,
):
    """Same column format as the CSV upload's sample file, so a downloaded
    export could be re-uploaded unchanged if useful — each row's real
    confirmation_type decides whether its time goes in 'scheduled for' or
    'schedule to send' (see confirmation_templates.has_scheduled_time)."""
    where = [Appointment.business_account_id == account.id]
    if search:
        like = f"%{search}%"
        where.append((Appointment.client_name.ilike(like)) | (Appointment.phone_number.ilike(like)))

    rows = (
        await db.execute(
            select(Appointment).where(*where).order_by(Appointment.scheduled_at)
        )
    ).scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["name", "phone number", "confirmation type", "reason", "scheduled for", "schedule to send", "language"])
    for appt in rows:
        has_time = templates.has_scheduled_time(appt.confirmation_type)
        formatted = appt.scheduled_at.strftime("%Y-%m-%d %H:%M")
        writer.writerow([
            appt.client_name, appt.phone_number, appt.confirmation_type.value,
            appt.subject_detail or "", formatted if has_time else "-", "-" if has_time else formatted,
            appt.language,
        ])
    return Response(
        content=buf.getvalue().encode("utf-8"), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=appointments_export.csv"},
    )


@router.get("/{appointment_id}", response_model=AppointmentOut)
async def get_appointment(
    appointment_id: uuid.UUID,
    account: BusinessAccount = Depends(current_business_account),
    db: AsyncSession = Depends(get_db),
):
    appt = await db.get(Appointment, appointment_id)
    if appt is None or appt.business_account_id != account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found")
    return appt


@router.delete("/{appointment_id}", response_model=AppointmentCancelOut)
async def cancel_appointment(
    appointment_id: uuid.UUID,
    account: BusinessAccount = Depends(current_business_account),
    db: AsyncSession = Depends(get_db),
):
    appt = await db.get(Appointment, appointment_id)
    if appt is None or appt.business_account_id != account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found")
    appt.status = AppointmentStatus.cancelled
    await db.commit()
    return AppointmentCancelOut(id=appt.id, status=appt.status.value)