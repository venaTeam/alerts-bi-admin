"""Protect decision parents and permit scoped whole-run deletion."""

from src.db.ledger import apply_sql

revision = "009_run_deletion"
down_revision = "008_measurement_basis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError("Add a new revision instead of rewriting applied decision protection")
