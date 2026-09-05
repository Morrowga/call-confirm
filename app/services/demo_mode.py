"""Demo Mode (Stage 1): exactly one capability — a single test call.

All rules enforced server-side:
  * target number MUST equal the account owner's own verified phone number;
  * lifetime cap of `demo_lifetime_call_cap` calls per account;
  * ~10 minute cooldown between calls per account;
  * combined calls/hour cap on the single shared demo number across ALL
    accounts, to keep carriers from spam-flagging it.
"""
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import DemoNumberUsage
from app.models.accounts import AccountStatus


def _normalize(number: str) -> str:
    return "".join(c for c in number if c.isdigit() or c == "+")


async def enforce_demo_rules(db: AsyncSession, account, target_number: str) -> None:
    if account.status != AccountStatus.demo:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Demo calls only available in Demo Mode")
    if not (account.email_verified and account.phone_verified):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Verify email and phone first")

    # Hard rule: self-call only. Reject any other target.
    if _normalize(target_number) != _normalize(account.phone_number):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Demo calls may only target your own verified phone number",
        )

    if account.demo_calls_used >= settings.demo_lifetime_call_cap:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Demo call limit reached")

    now = datetime.now(timezone.utc)
    if account.demo_last_call_at is not None:
        elapsed = now - account.demo_last_call_at
        cooldown = timedelta(minutes=settings.demo_cooldown_minutes)
        if elapsed < cooldown:
            wait = int((cooldown - elapsed).total_seconds() // 60) + 1
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"Please wait ~{wait} more minute(s) between demo calls",
            )

    if not settings.twilio_demo_number:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Demo number not configured")

    # Shared-number hourly cap across all accounts (row-locked upsert).
    bucket = now.replace(minute=0, second=0, microsecond=0)
    usage = (
        await db.execute(
            select(DemoNumberUsage)
            .where(DemoNumberUsage.hour_bucket == bucket)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if usage and usage.calls >= settings.demo_shared_number_calls_per_hour:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Demo line is at capacity right now — please try again shortly",
        )
    if usage:
        usage.calls += 1
    else:
        db.add(DemoNumberUsage(hour_bucket=bucket, calls=1))

    account.demo_calls_used += 1
    account.demo_last_call_at = now
    # caller commits alongside Call row creation
