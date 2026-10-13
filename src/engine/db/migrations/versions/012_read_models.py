"""Bounded read models, backfilled one complete run at a time."""

from alembic import op
from alerts_bi_shared.db.connection import Database

from src.db.ledger import apply_sql

revision = "012_read_models"
down_revision = "011_run_job_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)
    from src.db.read_models import rebuild_read_models

    db = Database(op.get_bind())
    for row in db.query("SELECT run_id FROM runs ORDER BY run_id"):
        rebuild_read_models(db, str(row["run_id"]))


def downgrade() -> None:
    raise NotImplementedError("Preserve additive read models; roll consumers back together")
