"""Add is_active to phone_number_pool — supports SIM/number changes that
keep every number an account has ever used as a permanent historical
record (never returned to the shared pool for reuse by another account,
avoiding caller-ID confusion for past recipients), with only one row
flagged active per account at a time for actual dispatch.

This is a NEW, distinct operation from the existing release_number (full
account cancellation — genuinely returns a number to the shared pool).
A "SIM change" never clears assigned_account_type/assigned_account_id on
the old row; it only flips is_active off there and on for the new one.
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_phone_number_pool_is_active"
down_revision = "0012_campaign_stripe_invoice_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "phone_number_pool",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    # Existing rows: exactly one per account today, so defaulting everyone
    # to True is correct — nothing to backfill selectively.


def downgrade() -> None:
    op.drop_column("phone_number_pool", "is_active")