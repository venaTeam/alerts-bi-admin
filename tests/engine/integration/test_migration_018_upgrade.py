"""Migration 018 on a populated 017 database: rows survive, and a failure leaves 017 intact."""

from __future__ import annotations

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
PREVIOUS_HEAD = "017_case_sensitive_teams"
KEY = "checkout-api:cart:node-1"
# With checkout-api and a 64-character run id, past the 384 characters the old keys allowed.
LONG_KEY = "k8s:prod-cluster/checkout/" + "pod-" * 100
COUNTED = (
    "alert_findings",
    "llm_verdicts",
    "finding_decisions",
    "run_alert_list",
    "run_summaries",
)


def _counts(db: Database) -> dict[str, int]:
    return {
        table: int(db.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"])  # type: ignore[index]
        for table in COUNTED
    }


def _key_columns(db: Database, index: str) -> list[str]:
    rows = db.query(
        "SELECT c.name FROM sys.indexes AS i "
        "JOIN sys.index_columns AS ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
        "JOIN sys.columns AS c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
        "WHERE i.name = :index ORDER BY ic.key_ordinal",
        {"index": index},
    )
    return [str(row["name"]) for row in rows]


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


def test_018_upgrades_a_populated_database_and_readiness_requires_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_to_previous_head(monkeypatch)
    run = sample_run()
    findings = [
        sample_finding(
            key_field=KEY,
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
        insert_verdict_row(db, sample_verdict(key_field=KEY))
        publish_run(db, str(run["run_id"]), published_by="upgrade-test", allow_gap=True)
        db.execute(
            "INSERT INTO finding_decisions (team_id, run_id, alert_schema, application, "
            "key_field, finding_id, state, note, decided_at, decided_by) VALUES "
            "(:team, :run, 'v1', 'checkout-api', :key, 'R1', 'confirmed', 'kept', "
            "SYSUTCDATETIME(), 'upgrade-test')",
            {"team": run["team_id"], "run": run["run_id"], "key": KEY},
        )
        db.commit()
        before = _counts(db)
        assert all(before.values()), before
        with pytest.raises(ValueError, match="018_alert_identity_hash"):
            check_schema(db)
        # The failure the run log reported: the old clustered key cannot hold this identity.
        with pytest.raises(Exception, match="1946"):
            insert_verdict_row(db, sample_verdict(key_field=LONG_KEY))
        db.rollback()

    applied, _already = migrations.migrate(CONFIG.sql, DB)
    assert applied == ["018_alert_identity_hash", "019_erase_operator_names"]

    with connect(CONFIG.sql, DB) as db:
        assert migrations.current_revision(db) == "019_erase_operator_names"
        assert _counts(db) == before
        check_schema(db)
        assert _key_columns(db, "pk_alert_findings") == ["run_id", "alert_schema", "identity_hash"]
        assert _key_columns(db, "pk_llm_verdicts") == [
            "identity_hash",
            "prompt_version",
            "model_version",
        ]
        # Upgraded rows are found through the hash the engine computes for its lookups.
        found = find_verdicts(db, "1.0.0", "fake-model-1", [("checkout-api", KEY)])
        assert [row["key_field"] for row in found.values()] == [KEY]
        decision = db.query(
            "SELECT note FROM portal_decisions WHERE application=N'checkout-api' AND key_field=:key",
            {"key": KEY},
        )
        assert decision == [{"note": "kept"}]

        # The identity that failed before the upgrade now persists with its verdict.
        long_run = sample_run(run_id="d" * 64)
        persist_run(
            db,
            PersistencePayload(
                run=long_run,
                findings=[sample_finding(run_id=long_run["run_id"], key_field=LONG_KEY)],
                verdicts=[sample_verdict(key_field=LONG_KEY)],
            ),
        )
        found = find_verdicts(db, "1.0.0", "fake-model-1", [("checkout-api", LONG_KEY)])
        assert [row["key_field"] for row in found.values()] == [LONG_KEY]
        stored = db.query_one(
            "SELECT COUNT(*) AS n FROM alert_findings WHERE run_id=:run AND key_field=:key",
            {"run": long_run["run_id"], "key": LONG_KEY},
        )
        assert stored == {"n": 1}


def test_a_failed_018_rolls_back_completely_and_a_retry_applies_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_to_previous_head(monkeypatch)
    with connect(CONFIG.sql, DB) as db:
        persist_run(db, PersistencePayload(run=sample_run(), findings=[sample_finding()]))
        insert_verdict_row(db, sample_verdict())
        # A conflicting column fails the second batch, after both keys were dropped.
        db.execute("ALTER TABLE llm_verdicts ADD identity_hash INT NULL")
        db.commit()
        before = _counts(db)

    try:
        with pytest.raises(Exception, match="identity_hash"):
            migrations.migrate(CONFIG.sql, DB)

        with connect(CONFIG.sql, DB) as db:
            assert migrations.current_revision(db) == PREVIOUS_HEAD
            ledger = db.query_one(
                "SELECT COUNT(*) AS n FROM schema_migrations "
                "WHERE version = N'018_alert_identity_hash'"
            )
            assert ledger == {"n": 0}
            assert _counts(db) == before
            assert _key_columns(db, "pk_alert_findings") == [
                "run_id",
                "alert_schema",
                "application",
                "key_field",
            ]
            assert _key_columns(db, "pk_llm_verdicts") == [
                "application",
                "key_field",
                "prompt_version",
                "model_version",
            ]
            added = db.query_one(
                "SELECT COUNT(*) AS n FROM sys.columns "
                "WHERE object_id = OBJECT_ID(N'alert_findings') AND name = N'identity_hash'"
            )
            assert added == {"n": 0}
    finally:
        # A failed assertion must not leave the blocker behind for later tests.
        with connect(CONFIG.sql, DB) as db:
            db.execute(
                "IF COL_LENGTH(N'llm_verdicts', N'identity_hash') = 4 "
                "ALTER TABLE llm_verdicts DROP COLUMN identity_hash"
            )
            db.commit()

    applied, _already = migrations.migrate(CONFIG.sql, DB)
    assert applied == ["018_alert_identity_hash", "019_erase_operator_names"]
    with connect(CONFIG.sql, DB) as db:
        assert migrations.current_revision(db) == "019_erase_operator_names"
        assert _counts(db) == before
