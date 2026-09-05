"""Add created_at to fan_contacts — genuinely missing entirely (a real bug:
list_contacts and the contact list UI both already referenced it, assuming
it existed, without it ever actually being added to the model or a prior
migration). Existing rows get backfilled to the migration run time, since
their real creation time was never recorded.
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_fan_contact_created_at"
down_revision = "0005_campaign_title"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fan_contacts",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("fan_contacts", "created_at")