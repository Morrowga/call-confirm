"""Make campaigns.scheduled_at nullable — a draft campaign now has no real
send time until the organizer actually clicks Send (or checkout), not at
creation time, so it can no longer be a required column from the start.
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_campaign_scheduled_at_nullable"
down_revision = "0007_campaign_scheduling"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("campaigns", "scheduled_at", nullable=True)


def downgrade() -> None:
    op.execute("UPDATE campaigns SET scheduled_at = NOW() WHERE scheduled_at IS NULL")
    op.alter_column("campaigns", "scheduled_at", nullable=False)