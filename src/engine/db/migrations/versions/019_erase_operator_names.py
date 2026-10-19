"""Erase stored operator names; the console no longer records who did what."""

from src.db.ledger import apply_sql

revision = "019_erase_operator_names"
down_revision = "018_alert_identity_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError(
        "Erased operator names cannot be restored; recover them only from a pre-019 backup"
    )
