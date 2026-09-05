"""Initial schema — creates all Phase 1 tables from model metadata.

Subsequent changes should use `alembic revision --autogenerate`.
"""
from alembic import op

from app.core.database import Base
import app.models  # noqa: F401  (registers all tables on Base.metadata)

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
