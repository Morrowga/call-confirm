"""Data retention tasks.

* Voice recordings: auto-export (email to account owner) then delete from S3
  once older than 90 days.
* Verified account-deletion requests: full data deletion within 30 days.
* Operational data (appointments, call logs) is retained indefinitely while
  the account remains active — no task touches it.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.core.config import settings
from app.core.database import CelerySessionLocal
from app.models import (
    Appointment, ApiKey, BusinessAccount, Call, CallResult, Campaign, Event,
    EventAccount, FanContact, VoiceMessage,
)
from app.models.accounts import AccountStatus
from app.services import notifications, number_provisioning, recording_storage
from app.tasks.celery_app import celery


def run(coro):
    return asyncio.run(coro)


@celery.task
def enforce_voice_message_retention():
    async def _enforce():
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.voice_message_retention_days)
        async with CelerySessionLocal() as db:
            expired = (
                await db.execute(
                    select(VoiceMessage).where(
                        VoiceMessage.created_at <= cutoff,
                        VoiceMessage.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
            if not expired:
                return
            for vm in expired:
                call = await db.get(Call, vm.call_id)
                account = (
                    await db.get(BusinessAccount, call.business_account_id)
                    if call and call.business_account_id else None
                )
                try:
                    if account and vm.exported_at is None:
                        audio = recording_storage.read_recording(vm.s3_key)
                        if audio is not None:
                            notifications.send_email(
                                account.email, "voice_message_export",
                                attachments=[(f"recording-{vm.id}.mp3", audio)],
                            )
                            vm.exported_at = datetime.now(timezone.utc)
                    recording_storage.delete_recording(vm.s3_key)
                    vm.deleted_at = datetime.now(timezone.utc)
                except Exception:
                    continue  # retried on next daily run
            await db.commit()
    run(_enforce())


@celery.task
def process_account_deletions():
    async def _process():
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.account_deletion_window_days)
        async with CelerySessionLocal() as db:
            for model, acct_type in ((BusinessAccount, "business"), (EventAccount, "event")):
                rows = (
                    await db.execute(
                        select(model).where(
                            model.status == AccountStatus.pending_deletion,
                            model.deletion_requested_at <= cutoff,
                        )
                    )
                ).scalars().all()
                for account in rows:
                    await _purge_account(db, account, acct_type)
            await db.commit()
    run(_process())


async def _purge_account(db, account, acct_type: str):
    await number_provisioning.release_number(db, acct_type, account.id)
    if acct_type == "business":
        call_filter = Call.business_account_id == account.id
        await db.execute(delete(ApiKey).where(ApiKey.business_account_id == account.id))
        appt_ids = select(Appointment.id).where(Appointment.business_account_id == account.id)
        await db.execute(delete(VoiceMessage).where(VoiceMessage.appointment_id.in_(appt_ids)))
        await db.execute(delete(Appointment).where(Appointment.business_account_id == account.id))
        event_filter = Event.business_account_id == account.id
    else:
        call_filter = Call.event_account_id == account.id
        event_filter = Event.event_account_id == account.id

    event_ids = select(Event.id).where(event_filter)
    await db.execute(delete(FanContact).where(FanContact.event_id.in_(event_ids)))
    await db.execute(delete(Campaign).where(Campaign.event_id.in_(event_ids)))
    await db.execute(delete(Event).where(event_filter))
    call_ids = select(Call.id).where(call_filter)
    await db.execute(delete(CallResult).where(CallResult.call_id.in_(call_ids)))
    await db.execute(delete(Call).where(call_filter))

    account.status = AccountStatus.deleted
    account.email = f"deleted+{account.id}@deleted.invalid"
    account.phone_number = ""
    account.password_hash = ""