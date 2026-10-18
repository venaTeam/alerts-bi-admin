"""Migration 015 upgrades a populated 014 database without losing or merging rows."""

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
PREVIOUS_HEAD = "014_backfills"
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


def test_015_upgrades_a_populated_database_and_readiness_requires_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def to_previous_head(sql: object, database: str) -> tuple[list[str], list[str]]:
        command.upgrade(migrations.alembic_config(CONFIG.sql, database), PREVIOUS_HEAD)
        return [], []

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "migrate", to_previous_head)
        try:
            migrations.reset_test_database(CONFIG.sql, DB)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"SQL Server is not reachable: {exc}")

    run = sample_run()
    findings = [
        sample_finding(
            application="Moby",
            key_field="disk:node-1",
            core_rule_ids="R1",
            quality_state="rule_flagged",
            llm_principle_id=None,
            llm_confidence=None,
            llm_justification=None,
        ),
        sample_finding(application="checkout-api"),
    ]
    with connect(CONFIG.sql, DB) as db:
        assert migrations.current_revision(db) == PREVIOUS_HEAD
        # Before 015 only one spelling can exist, as in a real pre-upgrade store.
        persist_run(
            db,
            PersistencePayload(
                run=run,
                findings=findings,
                verdicts=[sample_verdict()],
                application_daily=[{"run_id": run["run_id"], "application": "Moby", **_DAILY}],
            ),
        )
        publish_run(db, str(run["run_id"]), published_by="upgrade-test", allow_gap=True)
        db.execute(
            "INSERT INTO finding_decisions (team_id, run_id, alert_schema, application, "
            "key_field, finding_id, state, note, decided_at, decided_by) VALUES "
            "(:team, :run, 'v1', 'Moby', 'disk:node-1', 'R1', 'confirmed', 'kept', "
            "SYSUTCDATETIME(), 'upgrade-test')",
            {"team": run["team_id"], "run": run["run_id"]},
        )
        db.commit()
        before = _counts(db)
        assert all(before.values()), before
        with pytest.raises(ValueError, match="015_case_sensitive_applications"):
            check_schema(db)

    # Stop at 015 so this sees exactly what 015 did; 016 has its own upgrade test.
    command.upgrade(migrations.alembic_config(CONFIG.sql, DB), "015_case_sensitive_applications")

    with connect(CONFIG.sql, DB) as db:
        assert migrations.current_revision(db) == "015_case_sensitive_applications"
        assert _counts(db) == before
        # Readiness also needs 016 now; test_migration_016_upgrade.py applies it.
        with pytest.raises(ValueError, match="016_case_sensitive_key_fields"):
            check_schema(db)
        stored = db.query(
            "SELECT application FROM alert_findings WHERE run_id=:run ORDER BY application",
            {"run": run["run_id"]},
        )
        assert [row["application"] for row in stored] == ["Moby", "checkout-api"]
        decision = db.query_one(
            "SELECT note FROM portal_decisions WHERE application=N'Moby' AND key_field=N'disk:node-1'"
        )
        assert decision == {"note": "kept"}
        # The case-only variant that used to collide now fits beside the upgraded row.
        insert_verdict_if_absent(db, sample_verdict(application="Checkout-API"))
        db.commit()
        variants = db.query("SELECT application FROM llm_verdicts ORDER BY application")
        assert [row["application"] for row in variants] == ["Checkout-API", "checkout-api"]
