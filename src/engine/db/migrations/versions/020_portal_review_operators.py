"""portal_reviews names the operators each published week was run with."""

from src.db.ledger import apply_sql

revision = "020_portal_review_operators"
down_revision = "019_erase_operator_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError("Add a new revision instead of redefining a view readers use")
