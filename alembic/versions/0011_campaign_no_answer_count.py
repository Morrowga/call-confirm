"""Add no_answer_count to campaigns — same pattern as confirmed_count/
declined_count, tracking calls that were never picked up or were picked up
with no keypress response, per the unified no_answer vocabulary already
used on the Calls/Appointments pages.
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_campaign_no_answer_count"
down_revision = "0010_campaign_receipt_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("no_answer_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("campaigns", "no_answer_count")