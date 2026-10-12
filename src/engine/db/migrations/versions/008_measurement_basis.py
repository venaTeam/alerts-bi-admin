"""portal_reviews.basis_changed compares the team's own measurement, not the registry version."""

from src.db.ledger import apply_sql

revision = "008_measurement_basis"
down_revision = "007_portal_daily"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError("Add a new revision instead of redefining a view readers use")
