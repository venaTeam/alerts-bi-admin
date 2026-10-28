"""Revision 022 widens key_field and recreates the identity hash exactly as 018 defined it."""

from __future__ import annotations

import importlib.util

import pytest
from src.engine.db.ledger import MIGRATIONS_DIR
from src.engine.db.repositories import identity_hash_sql

SQL = MIGRATIONS_DIR / "022_long_key_fields.sql"
KEY_FIELD_TABLES = ("alert_findings", "llm_verdicts", "finding_decisions", "run_alert_list")


def test_022_refuses_downgrade() -> None:
    path = MIGRATIONS_DIR / "versions" / "022_long_key_fields.py"
    spec = importlib.util.spec_from_file_location("revision_022", path)
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)

    assert revision.down_revision == "021_parallel_run_slots"
    with pytest.raises(NotImplementedError, match="512"):
        revision.downgrade()


def test_recreated_hash_columns_are_what_the_lookups_compute() -> None:
    # A different expression would give every stored row a new hash, and the engine's lookups
    # would stop finding the stored verdicts.
    sql = SQL.read_text("utf-8")
    column = f"ADD identity_hash AS {identity_hash_sql('application', 'key_field')} PERSISTED"
    assert sql.count(f"ALTER TABLE alert_findings {column} NOT NULL;") == 1
    assert sql.count(f"ALTER TABLE llm_verdicts {column} NOT NULL;") == 1


def test_every_key_field_column_keeps_its_binary_collation_and_not_null() -> None:
    # ALTER COLUMN without COLLATE would restore the database's case-insensitive default.
    sql = SQL.read_text("utf-8")
    for table in KEY_FIELD_TABLES:
        assert (
            sql.count(
                f"ALTER TABLE {table} ALTER COLUMN key_field NVARCHAR(800) "
                "COLLATE Latin1_General_100_BIN2 NOT NULL;"
            )
            == 1
        ), table
