"""Add receipt_url to campaigns — the hosted Stripe receipt page for a
one-time campaign PaymentIntent charge. Subscription billing has real
Stripe Invoice objects with PDFs (see billing.py's invoice endpoints);
a one-time campaign charge doesn't, so this is the closest equivalent —
fetched once, at webhook time, when the charge is confirmed.
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_campaign_receipt_url"
down_revision = "0009_event_account_sim_fee"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("receipt_url", sa.String(length=1024), nullable=True))


def downgrade() -> None:
    op.drop_column("campaigns", "receipt_url")