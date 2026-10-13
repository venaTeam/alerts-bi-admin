"""Private resumable deterministic analysis."""

from src.db.ledger import apply_sql

revision = "013_analysis_checkpoints"
down_revision = "012_read_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError("Additive checkpoint storage can remain on consumer rollback")
