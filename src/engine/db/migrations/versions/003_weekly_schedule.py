"""The weekly schedule's outcome log.

Revision ID: 003_weekly_schedule
Revises: 002_review_portal

The DDL is in ``003_weekly_schedule.sql`` beside the ``versions`` directory (design sections
7.8 and 7.11).
"""

from __future__ import annotations

from src.db.ledger import apply_sql

revision = "003_weekly_schedule"
down_revision = "002_review_portal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError("003_weekly_schedule has no downgrade; add a new revision instead")
