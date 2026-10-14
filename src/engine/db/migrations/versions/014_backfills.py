"""Operator multi-week backfill and queued/cancelled run job states."""

from src.db.ledger import apply_sql

revision = "014_backfills"
down_revision = "013_analysis_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError(
        "Additive backfill state can remain installed on application rollback"
    )
