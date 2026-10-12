"""The portal_daily_metrics view: published weeks' UTC day buckets for the slides' charts."""

from src.db.ledger import apply_sql

revision = "007_portal_daily"
down_revision = "006_r6_episodes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError("Add a new revision instead of removing a view readers use")
