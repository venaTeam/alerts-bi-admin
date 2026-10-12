"""Durable pre-call requests, prompt provenance and recoverable LLM attempts."""

from src.db.ledger import apply_sql

revision = "004_llm_review_audit"
down_revision = "003_weekly_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError("Add a new revision instead of removing audit records")
