"""Preserve exact team identity across all stored team columns and portal views."""

from src.db.ledger import apply_sql

revision = "017_case_sensitive_teams"
down_revision = "016_case_sensitive_key_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError(
        "Case-only team IDs may exist after upgrade and would collide under "
        "a case-insensitive collation"
    )
