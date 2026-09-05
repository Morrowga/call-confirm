"""Add sim_fee_charged to event_accounts — one-time $3 SIM/number fee
charged on an event account's first-ever campaign checkout, tracked
explicitly rather than derived from phone-number-assignment state. Number
assignment happens later, at actual send/dispatch time (see
number_provisioning.py, still disabled while Twilio auto-purchase is off),
not at checkout — so deriving "already has a number" from that state would
risk double-charging a second campaign checked out before the first one's
number is actually assigned.
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_event_account_sim_fee"
down_revision = "0008_campaign_scheduled_at_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "event_accounts",
        sa.Column("sim_fee_charged", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("event_accounts", "sim_fee_charged")