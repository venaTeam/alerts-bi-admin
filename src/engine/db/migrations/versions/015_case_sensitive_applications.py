"""Exact, case-sensitive application identity in every stored application column."""

from src.db.ledger import apply_sql

revision = "015_case_sensitive_applications"
down_revision = "014_backfills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError(
        "Case-only application variants may exist after upgrade and would collide under "
        "a case-insensitive collation"
    )
