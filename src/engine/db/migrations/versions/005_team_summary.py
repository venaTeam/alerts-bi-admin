"""R6 firing facts, the `unseen` visibility measure and the portal views the team summary reads."""

from src.db.ledger import apply_sql

revision = "005_team_summary"
down_revision = "004_llm_review_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError("Add a new revision instead of removing stored facts")
