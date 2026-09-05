"""Add confirmation_type + subject_detail (appointments) and industry
(business_accounts) for the confirmation-type template system.

The initial migration uses Base.metadata.create_all(), which only creates
tables that don't yet exist — it won't add new columns to tables that were
already created. This migration adds them explicitly.
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_confirmation_types"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "business_accounts",
        sa.Column("industry", sa.String(length=32), nullable=False, server_default="general"),
    )
    op.add_column(
        "appointments",
        sa.Column("confirmation_type", sa.String(length=32), nullable=False, server_default="appointment"),
    )
    op.add_column(
        "appointments",
        sa.Column("subject_detail", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appointments", "subject_detail")
    op.drop_column("appointments", "confirmation_type")
    op.drop_column("business_accounts", "industry")