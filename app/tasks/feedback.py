"""Layer 4: continuous post-call behavioral feedback loop.

Every 30 minutes, recompute each active account's real decline ("press 2") and
abuse-report ("press 9") rates from completed calls. Exceeding either threshold
flips the account to manual-review-required for all future sends, regardless of
what its message content scores.
"""
import asyncio

from sqlalchemy import select

from app.core.database import CelerySessionLocal
from app.models import BusinessAccount, EventAccount
from app.models.accounts import AccountStatus
from app.services.risk import pipeline
from app.tasks.celery_app import celery


@celery.task
def run_feedback_loop():
    async def _run():
        async with CelerySessionLocal() as db:
            for model, acct_type in ((BusinessAccount, "business"), (EventAccount, "event")):
                accounts = (
                    await db.execute(
                        select(model).where(model.status == AccountStatus.active)
                    )
                ).scalars().all()
                for account in accounts:
                    await pipeline.run_feedback_loop_for_account(db, account, acct_type)
    asyncio.run(_run())