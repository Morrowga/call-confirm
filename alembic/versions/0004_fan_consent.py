"""Add consent_given_at to fan_contacts — records when a fan explicitly
self-consented via the public event signup form (/join/{event_id}), as a
real, checkable timestamp rather than only gating the checkbox client-side
and discarding it. Null for rows created via CSV upload, where consent is
instead attested by the uploading organizer at the batch level (see
BulkUpload.consent_attested) — a different person making the consent claim,
tracked separately rather than conflated into one flag.
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_fan_consent"
down_revision = "0003_proper_nouns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fan_contacts",
        sa.Column("consent_given_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("fan_contacts", "consent_given_at")