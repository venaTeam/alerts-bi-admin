"""Store key_field values up to 800 characters in every key_field column."""

from src.db.ledger import apply_sql

revision = "022_long_key_fields"
down_revision = "021_parallel_run_slots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError(
        "Keys longer than 512 characters may exist after upgrade and would not fit the "
        "restored NVARCHAR(512) columns"
    )
