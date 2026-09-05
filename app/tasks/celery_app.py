from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery = Celery(
    "callconfirm",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.calling", "app.tasks.retention", "app.tasks.feedback"],
)

celery.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    timezone="UTC",
    beat_schedule={
        # Scheduler: detect due appointment reminders (respecting each
        # account's timezone — stored as aware datetimes in UTC).
        "scan-due-appointments": {
            "task": "app.tasks.calling.scan_due_appointments",
            "schedule": 60.0,
        },
        # Same pattern — a campaign now waits for its own scheduled_at
        # instead of dispatching immediately on payment/approval.
        "scan-due-campaigns": {
            "task": "app.tasks.calling.scan_due_campaigns",
            "schedule": 60.0,
        },
        # Layer 4 behavioral feedback loop.
        "risk-feedback-loop": {
            "task": "app.tasks.feedback.run_feedback_loop",
            "schedule": crontab(minute="*/30"),
        },
        # 90-day voice message retention: export by email, then delete from S3.
        "voice-message-retention": {
            "task": "app.tasks.retention.enforce_voice_message_retention",
            "schedule": crontab(hour=3, minute=0),
        },
        # 30-day account deletion window.
        "account-deletion": {
            "task": "app.tasks.retention.process_account_deletions",
            "schedule": crontab(hour=4, minute=0),
        },
    },
)