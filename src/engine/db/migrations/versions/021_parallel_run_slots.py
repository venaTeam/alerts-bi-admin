"""Numbered run job slots, at most one per team, for parallel runs across teams."""

from src.db.ledger import apply_sql

revision = "021_parallel_run_slots"
down_revision = "020_portal_review_operators"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError(
        "Roll forward instead: WORKER_CONCURRENCY=1 runs one job at a time on this schema"
    )
