"""Exact idempotency-key equality for durable submissions."""

from src.db.ledger import apply_sql

revision = "011_run_job_keys"
down_revision = "010_run_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError("Preserve exact submission identities and the audit trail")
