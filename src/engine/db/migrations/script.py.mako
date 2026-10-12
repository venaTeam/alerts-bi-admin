"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Created: ${create_date}

The DDL belongs in a sibling ``.sql`` file named after this revision, so the checksum
ledger covers the text that actually runs. Keep this module to the two calls below.
"""

from __future__ import annotations

from src.db.ledger import apply_sql

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    apply_sql(revision)


def downgrade() -> None:
    raise NotImplementedError("this migration has no downgrade")
