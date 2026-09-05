"""Add scheduled_at and timezone to campaigns — a campaign now has a real
send time, validated at create/update time to never exceed the owning
event's release_deadline, instead of sending immediately whenever "Send"
is clicked. Existing rows are backfilled to their event's release_deadline
(the latest valid moment) so nothing ends up scheduled illegally after
this migration runs.
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_campaign_scheduling"
down_revision = "0006_fan_contact_created_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("campaigns", sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"))
    op.execute(
        "UPDATE campaigns SET scheduled_at = events.release_deadline "
        "FROM events WHERE campaigns.event_id = events.id AND campaigns.scheduled_at IS NULL"
    )
    op.alter_column("campaigns", "scheduled_at", nullable=False)


def downgrade() -> None:
    op.drop_column("campaigns", "timezone")
    op.drop_column("campaigns", "scheduled_at")