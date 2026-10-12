"""The read-only review portal: publications, human decisions, reader views and role.

Revision ID: 002_review_portal
Revises: 001_initial_schema

The DDL is in ``002_review_portal.sql`` beside the ``versions`` directory, so the checksum
ledger covers the text that actually ran (design sections 7.8 and 7.10).
"""

from __future__ import annotations

from src.db.ledger import apply_sql

revision = "002_review_portal"
down_revision = "001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    # Applied migrations are never edited or rolled back in place; add a new revision.
    raise NotImplementedError("002_review_portal has no downgrade; add a new revision instead")
