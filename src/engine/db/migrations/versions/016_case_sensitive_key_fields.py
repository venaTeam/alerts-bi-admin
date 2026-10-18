"""Exact, case-sensitive key_field identity in every stored key_field column."""

from src.db.ledger import apply_sql

revision = "016_case_sensitive_key_fields"
down_revision = "015_case_sensitive_applications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError(
        "Case-only key_field variants may exist after upgrade and would collide under "
        "a case-insensitive collation"
    )
