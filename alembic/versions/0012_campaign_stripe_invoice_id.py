"""Replace campaigns.receipt_url with campaigns.stripe_invoice_id — a
campaign is now billed as a real one-off Stripe Invoice, not a bare
PaymentIntent, so we store the invoice's own ID (set at checkout time)
instead of a separately-fetched hosted receipt URL (previously set at
webhook time). This lets campaign PDF downloads reuse the exact same
Stripe-generated invoice_pdf mechanism the subscription invoices already
use.
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_campaign_stripe_invoice_id"
down_revision = "0011_campaign_no_answer_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("stripe_invoice_id", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_campaigns_stripe_invoice_id", "campaigns", ["stripe_invoice_id"], unique=False,
    )
    op.drop_column("campaigns", "receipt_url")


def downgrade() -> None:
    op.add_column("campaigns", sa.Column("receipt_url", sa.String(length=1024), nullable=True))
    op.drop_index("ix_campaigns_stripe_invoice_id", table_name="campaigns")
    op.drop_column("campaigns", "stripe_invoice_id")