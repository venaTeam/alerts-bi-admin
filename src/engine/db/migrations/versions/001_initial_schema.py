"""The initial schema: runs, metrics, findings, LLM audit and panel parses.

Revision ID: 001_initial_schema
Revises: none - this is the first revision

The DDL is in ``001_initial_schema.sql`` beside the ``versions`` directory, unchanged from
the original migration runner, so a database migrated before Alembic was adopted verifies
against the same checksum.
"""

from __future__ import annotations

from src.db.ledger import apply_sql

revision = "001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    # The first revision's downgrade is dropping the database, which the guarded reset in
    # src.db.migrate does deliberately and only for the disposable test database.
    raise NotImplementedError("001_initial_schema has no downgrade; drop the database instead")
