"""R6 episode facts: the largest firing episode and when the open one began."""

from src.db.ledger import apply_sql

revision = "006_r6_episodes"
down_revision = "005_team_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError("Add a new revision instead of removing stored facts")
