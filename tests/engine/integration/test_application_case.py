"""Applications that differ only in case are separate identities in SQL, as in the engine.

Under the database's case-insensitive default collation, Moby and moby in one run collided
on the stored keys and the whole run failed to persist (migration 015).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alerts_bi_operations.review.decisions import record_decision
from alerts_bi_operations.review.publication import publish_run
from alerts_bi_shared.db.connection import Database, connect
from src.engine.config import load_config
from src.engine.db.migrate import reset_test_database
from src.engine.db.repositories import (
    PersistencePayload,
    find_verdicts,
    persist_run,
    verdict_key,
)
from src.engine.llm.fake import FakeLlmClient
from src.engine.run import orchestrator
from src.engine.run.orchestrator import execute_run
from src.engine.run.streaming import SchemaAccumulator
from src.queries import decision_history

from tests.engine.helpers.rows import v1_row

pytestmark = pytest.mark.integration

CONFIG = load_config()
RUN_AT = datetime(2026, 8, 25, 18, 0, 0, tzinfo=UTC)
DAY = "2026-08-20T12:00:00.000Z"
# One key_field for both, so even alert_findings' (application, key_field) key is shared.
KEY = "disk:node-1"
APPLICATIONS = ("Moby", "moby")
EXPECTED = sorted(APPLICATIONS)
# R1's generic phrase gives each alert a rule finding a decision can name.
GENERIC = "Error occurred"
BINARY = "Latin1_General_100_BIN2"

# Every stored application column, and every reader view that exposes one.
APPLICATION_TABLES = (
    "alert_findings",
    "llm_verdicts",
    "finding_decisions",
    "run_alert_list",
    "run_summaries",
    "run_application_daily",
    "run_v1_rules",
)
APPLICATION_VIEWS = (
    "portal_alerts",
    "portal_decisions",
    "portal_alert_list",
    "portal_application_daily",
    "portal_run_summaries",
    "portal_v1_rules",
)


@pytest.fixture(scope="module")
def db() -> Iterator[Database]:
    try:
        reset_test_database(CONFIG.sql, CONFIG.sql.test_database)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"SQL Server is not reachable: {exc}")
    with connect(CONFIG.sql, CONFIG.sql.test_database) as connection:
        yield connection


def _registry(tmp_path: Path, version: str) -> str:
    # The registry version is part of run_id, so each test owns its run.
    document = {
        "registry_version": version,
        "effective_date": "2026-10-18",
        "teams": [
            {
                "team_id": "case-team",
                "display_name": "Case Team",
                "v1_operators": ["team-op"],
                "v2_operator": "team-op-v2",
            }
        ],
    }
    path = tmp_path / "teams.json"
    path.write_text(json.dumps(document))
    return str(path)


def _execute(
    monkeypatch: pytest.MonkeyPatch,
    registry: str,
    db: Database,
    llm: FakeLlmClient,
    **row: Any,
) -> PersistencePayload:
    rows = [
        v1_row(application=application, key_field=KEY, **{"@timestamp": DAY}, **row)
        for application in APPLICATIONS
    ]

    def fake_read(_client: Any, team: Any, window: Any) -> dict[str, SchemaAccumulator]:
        result = {}
        for schema, schema_rows in (("v1", rows), ("v2", [])):
            analysis = SchemaAccumulator(schema, window, team.panels_for(schema))
            for item in schema_rows:
                analysis.add(item)
            analysis.finish()
            result[schema] = analysis
        return result

    monkeypatch.setattr(orchestrator, "analyze_team", fake_read)
    payload, _summary = execute_run(
        team_id="case-team",
        run_at=RUN_AT,
        config=CONFIG,
        es_client=object(),  # type: ignore[arg-type]
        llm_client=llm,
        registry_path=registry,
        db=db,
    )
    assert sorted(finding["application"] for finding in payload.findings) == EXPECTED
    return payload


def _applications(db: Database, table: str, run_id: str, where: str = "") -> list[str]:
    rows = db.query(f"SELECT application FROM {table} WHERE run_id=:run {where}", {"run": run_id})
    return sorted(str(row["application"]) for row in rows)


def test_case_only_variants_persist_publish_and_take_decisions_separately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db: Database
) -> None:
    payload = _execute(
        monkeypatch, _registry(tmp_path, "2026-10-18.1"), db, FakeLlmClient(), message=GENERIC
    )
    run_id = str(payload.run["run_id"])
    persist_run(db, payload)

    one_row_each = ("alert_findings", "run_alert_list", "run_v1_rules")
    for table in one_row_each:
        assert _applications(db, table, run_id) == EXPECTED, table
    assert _applications(db, "run_summaries", run_id, "AND scope_kind='application'") == EXPECTED
    # Every window day has a row per application; both alerted on the same day.
    alerted = "AND snapshot_date='2026-08-20' AND events > 0"
    assert _applications(db, "run_application_daily", run_id, alerted) == EXPECTED

    # Equality is exact: one spelling never matches the other.
    for application in APPLICATIONS:
        rows = db.query(
            "SELECT application FROM alert_findings WHERE run_id=:run AND application=:app",
            {"run": run_id, "app": application},
        )
        assert [row["application"] for row in rows] == [application]

    publish_run(db, run_id, published_by="case-test", allow_gap=True)
    for application in APPLICATIONS:
        finding = next(f for f in payload.findings if f["application"] == application)
        finding_id = str(finding["core_rule_ids"]).split(",")[0]
        assert finding_id, "the generic message must raise a rule finding"
        record_decision(
            db,
            run_id=run_id,
            alert_schema="v1",
            application=application,
            key_field=KEY,
            finding_id=finding_id,
            state="confirmed",
            note=f"Reviewed {application}",
            decided_by="case-test",
        )

    # Readers see both spellings through every view that exposes application.
    for view in ("portal_alerts", "portal_alert_list", "portal_v1_rules"):
        assert _applications(db, view, run_id) == EXPECTED, view
    decided = db.query("SELECT application FROM portal_decisions WHERE team_id=N'case-team'")
    assert sorted(str(row["application"]) for row in decided) == EXPECTED
    assert (
        _applications(db, "portal_run_summaries", run_id, "AND scope_kind='application'")
        == EXPECTED
    )
    assert _applications(db, "portal_application_daily", run_id, alerted) == EXPECTED

    # The OPENJSON identity lookup returns only the exact spelling's decision.
    for application in APPLICATIONS:
        identity = ("v1", application, KEY)
        history = decision_history(db, [identity], "case-team")
        assert [row["note"] for row in history[identity]] == [f"Reviewed {application}"]
        assert history[identity][0]["application"] == application


def test_each_case_variant_reuses_only_its_own_stored_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db: Database
) -> None:
    registry = _registry(tmp_path, "2026-10-18.2")
    first = _execute(monkeypatch, registry, db, FakeLlmClient())
    persist_run(db, first)

    verdicts = find_verdicts(
        db,
        str(first.run["prompt_version"]),
        str(first.run["model_version"]),
        [(application, KEY) for application in APPLICATIONS],
    )
    for application in APPLICATIONS:
        stored = verdicts[verdict_key(application, KEY)]
        assert stored["application"] == application

    rerun_llm = FakeLlmClient()
    rerun = _execute(monkeypatch, registry, db, rerun_llm)
    assert rerun_llm.calls == []
    by_application = {finding["application"]: finding for finding in rerun.findings}
    for application in APPLICATIONS:
        stored = verdicts[verdict_key(application, KEY)]
        assert by_application[application]["llm_principle_id"] == stored["principle_id"]
    persist_run(db, rerun)


def test_every_stored_application_column_compares_exactly(db: Database) -> None:
    rows = db.query(
        "SELECT OBJECT_NAME(object_id) AS owner, collation_name FROM sys.columns "
        "WHERE name = N'application'"
    )
    collations = {str(row["owner"]): row["collation_name"] for row in rows}
    assert set(APPLICATION_TABLES + APPLICATION_VIEWS) <= set(collations)
    # A later migration must not silently restore a case-insensitive collation.
    assert {
        owner: collation for owner, collation in collations.items() if collation != BINARY
    } == {}
