"""Migration 016 on a populated 015 database: rows survive, and a failure leaves 015 intact."""

from __future__ import annotations

import pytest
from alembic import command
from alerts_bi_operations.review.publication import publish_run
from alerts_bi_shared.db.connection import Database, connect
from src.engine.config import load_config
from src.engine.db import migrate as migrations
from src.engine.db.repositories import (
    PersistencePayload,
    insert_verdict_if_absent,
    persist_run,
)
from src.readiness import check_schema

from tests.engine.helpers.sql import sample_finding, sample_run, sample_verdict

_DAILY = {
    "alert_schema": "v1",
    "snapshot_date": "2026-08-20",
    "covered_hours": 24,
    "events": 4,
    "distinct_alerts": 1,
    "flagged_rows": 4,
    "flagged_by_rule_distinct": 1,
    "suppressed": 0,
    "unseen": None,
}

pytestmark = pytest.mark.integration

CONFIG = load_config()
DB = CONFIG.sql.test_database
PREVIOUS_HEAD = "015_case_sensitive_applications"
KEY = "checkout-api:cart:node-1"
COUNTED = (
    "alert_findings",
    "llm_verdicts",
    "finding_decisions",
    "run_alert_list",
    "run_summaries",
    "run_application_daily",
    "run_v1_rules",
)


def _counts(db: Database) -> dict[str, int]:
    return {
        table: int(db.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"])  # type: ignore[index]
        for table in COUNTED
    }


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


def test_016_upgrades_a_populated_database_and_readiness_requires_it(
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
        # Before 016 only one spelling of a key can exist, as in a real pre-upgrade store.
        persist_run(
            db,
            PersistencePayload(
                run=run,
                findings=findings,
                verdicts=[sample_verdict(key_field=KEY)],
                application_daily=[
                    {"run_id": run["run_id"], "application": "checkout-api", **_DAILY}
                ],
            ),
        )
        publish_run(db, str(run["run_id"]), allow_gap=True)
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
        with pytest.raises(ValueError, match="016_case_sensitive_key_fields"):
            check_schema(db)

    applied, _already = migrations.migrate(CONFIG.sql, DB)
    assert applied == [
        "016_case_sensitive_key_fields",
        "017_case_sensitive_teams",
        "018_erase_operator_names",
    ]

    with connect(CONFIG.sql, DB) as db:
        assert migrations.current_revision(db) == "018_erase_operator_names"
        assert _counts(db) == before
        check_schema(db)
        stored = db.query(
            "SELECT key_field FROM alert_findings WHERE run_id=:run ORDER BY key_field",
            {"run": run["run_id"]},
        )
        assert [row["key_field"] for row in stored] == [KEY, "checkout-api:payments:node-2"]
        decision = db.query(
            "SELECT note FROM portal_decisions WHERE application=N'checkout-api' AND key_field=:key",
            {"key": KEY},
        )
        assert decision == [{"note": "kept"}]
        assert (
            db.query_one(
                "SELECT 1 AS found FROM portal_decisions WHERE key_field=:key",
                {"key": KEY.replace("node-1", "Node-1")},
            )
            is None
        )
        # The case-only key variant that used to be skipped now fits beside the upgraded row.
        insert_verdict_if_absent(db, sample_verdict(key_field=KEY.replace("node-1", "Node-1")))
        db.commit()
        variants = db.query("SELECT key_field FROM llm_verdicts ORDER BY key_field")
        assert [row["key_field"] for row in variants] == [
            "checkout-api:cart:Node-1",
            "checkout-api:cart:node-1",
        ]


def test_a_failed_016_rolls_back_completely_and_a_retry_applies_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_to_previous_head(monkeypatch)
    with connect(CONFIG.sql, DB) as db:
        persist_run(
            db,
            PersistencePayload(
                run=sample_run(), findings=[sample_finding()], verdicts=[sample_verdict()]
            ),
        )
        # A schema-bound reference blocks the column change after the keys were dropped.
        db.execute(
            "CREATE VIEW dbo.blocks_016 WITH SCHEMABINDING AS "
            "SELECT key_field FROM dbo.alert_findings"
        )
        db.commit()
        before = _counts(db)

    try:
        with pytest.raises(Exception, match="blocks_016"):
            migrations.migrate(CONFIG.sql, DB)

        with connect(CONFIG.sql, DB) as db:
            assert migrations.current_revision(db) == PREVIOUS_HEAD
            ledger = db.query_one(
                "SELECT COUNT(*) AS n FROM schema_migrations "
                "WHERE version = N'016_case_sensitive_key_fields'"
            )
            assert ledger == {"n": 0}
            assert _counts(db) == before
            restored = db.query(
                "SELECT name FROM sys.indexes WHERE name IN (N'pk_alert_findings', "
                "N'pk_llm_verdicts', N'ix_finding_decisions_identity', "
                "N'ix_run_alert_list_order', N'ix_run_alert_list_events')"
            )
            assert len(restored) == 5
            collation = db.query_one(
                "SELECT collation_name FROM sys.columns "
                "WHERE object_id = OBJECT_ID(N'alert_findings') AND name = N'key_field'"
            )
            assert collation is not None
            assert collation["collation_name"] != "Latin1_General_100_BIN2"
    finally:
        # A failed assertion must not leave the blocker behind for later tests.
        with connect(CONFIG.sql, DB) as db:
            db.execute("DROP VIEW IF EXISTS dbo.blocks_016")
            db.commit()

    applied, _already = migrations.migrate(CONFIG.sql, DB)
    assert applied == [
        "016_case_sensitive_key_fields",
        "017_case_sensitive_teams",
        "018_erase_operator_names",
    ]
    with connect(CONFIG.sql, DB) as db:
        assert migrations.current_revision(db) == "018_erase_operator_names"
        assert _counts(db) == before
