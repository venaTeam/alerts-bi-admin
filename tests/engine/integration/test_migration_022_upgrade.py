"""Migration 022 on a populated 021 database: rows and hashes survive, and a failure leaves 021
intact."""

from __future__ import annotations

from typing import Any

import pytest
from alembic import command
from alerts_bi_operations.review.publication import publish_run
from alerts_bi_shared.db.connection import Database, connect
from src.engine.config import load_config
from src.engine.db import migrate as migrations
from src.engine.db.repositories import (
    PersistencePayload,
    find_verdicts,
    persist_run,
)
from src.readiness import check_schema

from tests.engine.helpers.sql import (
    insert_verdict_row,
    sample_finding,
    sample_run,
    sample_verdict,
)

pytestmark = pytest.mark.integration

CONFIG = load_config()
DB = CONFIG.sql.test_database
PREVIOUS_HEAD = "021_parallel_run_slots"
# The widest key the old columns held, and the widest the new ones hold.
OLD_WIDEST = ("k8s:prod/checkout/pod-" * 30)[:512]
LONG_KEY = ("k8s:prod/checkout/pod-" * 40)[:800]
KEY_FIELD_TABLES = ("alert_findings", "llm_verdicts", "finding_decisions", "run_alert_list")
KEY_FIELD_VIEWS = ("portal_alerts", "portal_decisions", "portal_alert_list")
COUNTED = (
    "alert_findings",
    "llm_verdicts",
    "finding_decisions",
    "run_alert_list",
    "run_summaries",
)
# Each index's key columns in order, then its included columns.
OLD_INDEXES: dict[str, tuple[list[str], list[str]]] = {
    "ix_finding_decisions_identity": (
        ["alert_schema", "application", "key_field", "finding_id", "decided_at"],
        [],
    ),
    "ix_run_alert_list_order": (
        ["run_id", "attention_rank", "row_count", "alert_schema", "application", "key_field"],
        [],
    ),
    "ix_run_alert_list_events": (
        ["run_id", "row_count", "application", "key_field", "alert_schema"],
        [],
    ),
}
NEW_INDEXES: dict[str, tuple[list[str], list[str]]] = {
    "ix_finding_decisions_identity": (
        ["alert_schema", "application", "finding_id", "decided_at"],
        ["key_field"],
    ),
    "ix_run_alert_list_order": (
        ["run_id", "attention_rank", "row_count", "alert_schema", "application"],
        ["key_field"],
    ),
    "ix_run_alert_list_events": (
        ["run_id", "row_count", "application", "alert_schema"],
        ["key_field"],
    ),
}
BLOCKER = "blocks_022"


def _counts(db: Database) -> dict[str, int]:
    return {
        table: int(db.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"])  # type: ignore[index]
        for table in COUNTED
    }


def _hashes(db: Database) -> dict[str, list[tuple[Any, ...]]]:
    return {
        "alert_findings": [
            (row["run_id"], row["key_field"], bytes(row["identity_hash"]))
            for row in db.query(
                "SELECT run_id, key_field, identity_hash FROM alert_findings "
                "ORDER BY run_id, identity_hash"
            )
        ],
        "llm_verdicts": [
            (row["key_field"], bytes(row["identity_hash"]))
            for row in db.query(
                "SELECT key_field, identity_hash FROM llm_verdicts ORDER BY identity_hash"
            )
        ],
    }


def _index(db: Database, index: str) -> tuple[list[str], list[str]]:
    rows = db.query(
        "SELECT c.name, ic.is_included_column FROM sys.indexes AS i "
        "JOIN sys.index_columns AS ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
        "JOIN sys.columns AS c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
        "WHERE i.name = :index ORDER BY ic.key_ordinal, c.name",
        {"index": index},
    )
    keys = [str(row["name"]) for row in rows if not row["is_included_column"]]
    included = [str(row["name"]) for row in rows if row["is_included_column"]]
    return keys, included


def _key_field_columns(db: Database) -> dict[str, tuple[Any, ...]]:
    rows = db.query(
        "SELECT TABLE_NAME AS owner, CHARACTER_MAXIMUM_LENGTH AS width, "
        "COLLATION_NAME AS collation, IS_NULLABLE AS nullable "
        "FROM INFORMATION_SCHEMA.COLUMNS WHERE COLUMN_NAME = N'key_field'"
    )
    return {str(row["owner"]): (row["width"], row["collation"], row["nullable"]) for row in rows}


def _reset_to_previous_head(monkeypatch: pytest.MonkeyPatch) -> None:
    def to_previous_head(sql: object, database: str) -> tuple[list[str], list[str]]:
        command.upgrade(migrations.alembic_config(CONFIG.sql, database), PREVIOUS_HEAD)
        return [], []

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "migrate", to_previous_head)
        try:
            migrations.reset_test_database(CONFIG.sql, DB)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"SQL Server is not reachable: {exc}")


def test_022_upgrades_a_populated_database_and_readiness_requires_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_to_previous_head(monkeypatch)
    run = sample_run()
    findings = [
        sample_finding(
            key_field=OLD_WIDEST,
            core_rule_ids="R1",
            quality_state="rule_flagged",
            llm_principle_id=None,
            llm_confidence=None,
            llm_justification=None,
        ),
        sample_finding(key_field="checkout-api:payments:node-2"),
    ]
    with connect(CONFIG.sql, DB) as db:
        assert migrations.current_revision(db) == PREVIOUS_HEAD
        persist_run(db, PersistencePayload(run=run, findings=findings))
        insert_verdict_row(db, sample_verdict(key_field=OLD_WIDEST))
        publish_run(db, str(run["run_id"]), allow_gap=True)
        db.execute(
            "INSERT INTO finding_decisions (team_id, run_id, alert_schema, application, "
            "key_field, finding_id, state, note, decided_at, decided_by) VALUES "
            "(:team, :run, 'v1', 'checkout-api', :key, 'R1', 'confirmed', 'kept', "
            "SYSUTCDATETIME(), '')",
            {"team": run["team_id"], "run": run["run_id"], "key": OLD_WIDEST},
        )
        db.commit()
        before = _counts(db)
        hashes = _hashes(db)
        assert all(before.values()), before
        with pytest.raises(ValueError, match="022_long_key_fields"):
            check_schema(db)
        # The failure the sending team hit: the old columns cannot hold a longer key.
        with pytest.raises(Exception, match="truncated"):
            insert_verdict_row(db, sample_verdict(key_field=LONG_KEY))
        db.rollback()

    applied, _already = migrations.migrate(CONFIG.sql, DB)
    assert applied == ["022_long_key_fields"]

    with connect(CONFIG.sql, DB) as db:
        assert migrations.current_revision(db) == "022_long_key_fields"
        assert _counts(db) == before
        # The hash covers the value, not the declared width: every stored identity keeps it.
        assert _hashes(db) == hashes
        check_schema(db)
        columns = _key_field_columns(db)
        for owner in KEY_FIELD_TABLES + KEY_FIELD_VIEWS:
            assert columns[owner] == (800, "Latin1_General_100_BIN2", "NO"), owner
        assert _index(db, "pk_alert_findings") == (["run_id", "alert_schema", "identity_hash"], [])
        assert _index(db, "pk_llm_verdicts") == (
            ["identity_hash", "prompt_version", "model_version"],
            [],
        )
        for index, expected in NEW_INDEXES.items():
            assert _index(db, index) == expected, index

        found = find_verdicts(db, "1.0.0", "fake-model-1", [("checkout-api", OLD_WIDEST)])
        assert [row["key_field"] for row in found.values()] == [OLD_WIDEST]
        decision = db.query(
            "SELECT note FROM portal_decisions WHERE application=N'checkout-api' AND key_field=:key",
            {"key": OLD_WIDEST},
        )
        assert decision == [{"note": "kept"}]

        # A key the old columns refused now persists with its verdict and reaches the readers.
        long_run = sample_run(run_id="d" * 64)
        persist_run(
            db,
            PersistencePayload(
                run=long_run,
                findings=[sample_finding(run_id=long_run["run_id"], key_field=LONG_KEY)],
                verdicts=[sample_verdict(key_field=LONG_KEY)],
            ),
        )
        db.commit()
        found = find_verdicts(db, "1.0.0", "fake-model-1", [("checkout-api", LONG_KEY)])
        assert [row["key_field"] for row in found.values()] == [LONG_KEY]
        for table in ("alert_findings", "run_alert_list"):
            stored = db.query_one(
                f"SELECT COUNT(*) AS n FROM {table} WHERE run_id=:run AND key_field=:key",
                {"run": long_run["run_id"], "key": LONG_KEY},
            )
            assert stored == {"n": 1}, table


def test_a_failed_022_rolls_back_completely_and_a_retry_applies_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_to_previous_head(monkeypatch)
    with connect(CONFIG.sql, DB) as db:
        persist_run(db, PersistencePayload(run=sample_run(), findings=[sample_finding()]))
        insert_verdict_row(db, sample_verdict())
        # A schema-bound view fails the last ALTER COLUMN, after the keys, indexes and hash
        # columns were dropped and three columns widened.
        db.execute(
            f"CREATE VIEW {BLOCKER} WITH SCHEMABINDING AS SELECT key_field FROM dbo.run_alert_list"
        )
        db.commit()
        before = _counts(db)
        hashes = _hashes(db)
        columns = _key_field_columns(db)

    try:
        with pytest.raises(Exception, match=BLOCKER):
            migrations.migrate(CONFIG.sql, DB)

        with connect(CONFIG.sql, DB) as db:
            assert migrations.current_revision(db) == PREVIOUS_HEAD
            ledger = db.query_one(
                "SELECT COUNT(*) AS n FROM schema_migrations WHERE version = N'022_long_key_fields'"
            )
            assert ledger == {"n": 0}
            assert _counts(db) == before
            assert _hashes(db) == hashes
            assert _key_field_columns(db) == columns
            assert columns["alert_findings"][0] == 512
            assert _index(db, "pk_alert_findings") == (
                ["run_id", "alert_schema", "identity_hash"],
                [],
            )
            for index, expected in OLD_INDEXES.items():
                assert _index(db, index) == expected, index
    finally:
        # A failed assertion must not leave the blocker behind for later tests.
        with connect(CONFIG.sql, DB) as db:
            db.execute(f"DROP VIEW IF EXISTS {BLOCKER}")
            db.commit()

    applied, _already = migrations.migrate(CONFIG.sql, DB)
    assert applied == ["022_long_key_fields"]
    with connect(CONFIG.sql, DB) as db:
        assert migrations.current_revision(db) == "022_long_key_fields"
        assert _counts(db) == before
        assert _hashes(db) == hashes
