"""Add subject_detail_proper_nouns to appointments — persists AI-identified
proper-noun spans (names/places/brands) from creation time, so the actual
call can wrap them in an SSML <lang> tag without needing a fresh AI call at
dial time.
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_proper_nouns"
down_revision = "0002_confirmation_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appointments",
        sa.Column("subject_detail_proper_nouns", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appointments", "subject_detail_proper_nouns")