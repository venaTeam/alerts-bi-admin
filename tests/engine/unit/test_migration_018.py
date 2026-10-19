"""Revision 018 keys identities by a hash that the engine's lookups compute identically."""

from __future__ import annotations

import importlib.util

import pytest
from src.engine.db.ledger import MIGRATIONS_DIR
from src.engine.db.repositories import identity_hash_sql


def test_018_refuses_downgrade() -> None:
    path = MIGRATIONS_DIR / "versions" / "018_alert_identity_hash.py"
    spec = importlib.util.spec_from_file_location("revision_018", path)
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)

    assert revision.down_revision == "017_case_sensitive_teams"
    with pytest.raises(NotImplementedError, match="900-byte"):
        revision.downgrade()


def test_lookups_hash_exactly_what_the_computed_columns_store() -> None:
    # A lookup that hashed differently would never seek a stored row: every rerun would ask
    # the model again and then collide with the verdict it failed to find.
    sql = (MIGRATIONS_DIR / "018_alert_identity_hash.sql").read_text("utf-8")
    column = f"ADD identity_hash AS {identity_hash_sql('application', 'key_field')} PERSISTED"
    assert sql.count(f"ALTER TABLE alert_findings {column} NOT NULL;") == 1
    assert sql.count(f"ALTER TABLE llm_verdicts {column} NOT NULL;") == 1
