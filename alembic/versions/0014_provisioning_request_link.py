"""Link PhoneNumberPool to the ProvisioningRequest it fulfills, and widen
ProvisioningRequest.status to fit the new "ready_for_payment" value.

Supports the self-service additional-SIM flow: an admin manually purchases
a real number for a request (see number_pool.py's approve endpoint), which
creates a PhoneNumberPool row linked back to that request via
provisioning_request_id — is_active stays False on that row until the
account holder actually pays the one-time fee (see billing.py's new
/numbers/pending-request/{id}/pay endpoint), at which point it's activated.

status column was String(16) — "ready_for_payment" is 18 characters, which
would have hit the exact same "value too long for type character varying"
error this project already ran into twice before (alembic_version,
event_accounts.status). Widened this time BEFORE it becomes a live bug
rather than after.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0014_provisioning_request_link"
down_revision = "0013_phone_number_pool_is_active"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "provisioning_requests", "status",
        type_=sa.String(length=32), existing_type=sa.String(length=16),
    )
    op.add_column(
        "provisioning_requests",
        sa.Column("is_additional", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "phone_number_pool",
        sa.Column("provisioning_request_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_phone_number_pool_provisioning_request",
        "phone_number_pool", "provisioning_requests",
        ["provisioning_request_id"], ["id"],
    )
    op.create_index(
        "ix_phone_number_pool_provisioning_request_id",
        "phone_number_pool", ["provisioning_request_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_phone_number_pool_provisioning_request_id", table_name="phone_number_pool")
    op.drop_constraint("fk_phone_number_pool_provisioning_request", "phone_number_pool", type_="foreignkey")
    op.drop_column("phone_number_pool", "provisioning_request_id")
    op.drop_column("provisioning_requests", "is_additional")
    op.alter_column(
        "provisioning_requests", "status",
        type_=sa.String(length=16), existing_type=sa.String(length=32),
    )