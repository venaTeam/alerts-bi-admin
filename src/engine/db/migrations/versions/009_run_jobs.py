"""Durable asynchronous manual run submissions."""

from src.db.ledger import apply_sql

revision = "009_run_jobs"
down_revision = "008_measurement_basis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError(
        "Preserve the submission audit trail; roll back application code only"
    )
