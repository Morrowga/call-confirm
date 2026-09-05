"""Add title to campaigns — so a campaign can actually be identified in the
list (previously only showed state/counts, with no way to tell them apart
at a glance). Existing rows get a generic placeholder title since there's
no real title to backfill from.
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_campaign_title"
down_revision = "0004_fan_consent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("title", sa.String(255), nullable=True))
    op.execute("UPDATE campaigns SET title = 'Untitled campaign' WHERE title IS NULL")
    op.alter_column("campaigns", "title", nullable=False)


def downgrade() -> None:
    op.drop_column("campaigns", "title")