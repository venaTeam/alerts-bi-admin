"""Key alert findings and durable verdicts by a fixed-width identity hash."""

from src.db.ledger import apply_sql

revision = "018_alert_identity_hash"
down_revision = "017_case_sensitive_teams"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError(
        "Identities longer than the old 900-byte keys may exist after upgrade and would not "
        "fit the restored keys"
    )
