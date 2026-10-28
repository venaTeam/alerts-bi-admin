"""Key fields that differ only in case are separate identities in SQL, as in the engine.

Under the database's case-insensitive default collation, Node-1 and node-1 within one exact
application collided on the stored keys and the whole run failed to persist (migration 016).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alerts_bi_operations.review.decisions import findings_on, record_decision
from alerts_bi_operations.review.publication import publish_run
from alerts_bi_shared.db.connection import Database, connect
from src.engine.config import load_config
from src.engine.db.migrate import reset_test_database
from src.engine.db.repositories import PersistencePayload, find_verdicts, persist_run
from src.engine.llm.fake import FakeLlmClient, scripted_verdicts
from src.engine.run import orchestrator
from src.engine.run.orchestrator import execute_run
from src.engine.run.streaming import SchemaAccumulator

from tests.engine.helpers.rows import v1_row

pytestmark = pytest.mark.integration

CONFIG = load_config()
RUN_AT = datetime(2026, 8, 25, 18, 0, 0, tzinfo=UTC)
DAY = "2026-08-20T12:00:00.000Z"
# One exact application for both, so only key_field tells the identities apart.
APPLICATION = "case-app"
KEYS = {"disk:Node-1", "disk:node-1"}
TEAM = "key-case-team"
BINARY = "Latin1_General_100_BIN2"

# Every stored key_field column, and every reader view that exposes one.
KEY_FIELD_TABLES = ("alert_findings", "llm_verdicts", "finding_decisions", "run_alert_list")
KEY_FIELD_VIEWS = ("portal_alerts", "portal_decisions", "portal_alert_list")
# Every key and index that contains key_field, which 016 drops and recreates, with whether each
# is the primary key. Migration 018 then keys alert_findings and llm_verdicts by identity_hash
# instead of key_field, and 022 carries key_field in these three as an included column.
REBUILT_KEYS = {
    ("finding_decisions", "ix_finding_decisions_identity"): False,
    ("run_alert_list", "ix_run_alert_list_order"): False,
    ("run_alert_list", "ix_run_alert_list_events"): False,
}


def _model() -> FakeLlmClient:
    # A raised verdict gives each identity a finding a reader can decide on.
    return FakeLlmClient(
        fallback=scripted_verdicts(
            lambda _alert, _index, _request: {
                "assessment": "other",
                "principle_id": "OTHER",
                "confidence": "medium",
                "justification": "Deterministic fake: needs a reviewer.",
            }
        )
    )


@pytest.fixture(scope="module")
def db() -> Iterator[Database]:
    try:
        reset_test_database(CONFIG.sql, CONFIG.sql.test_database)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"SQL Server is not reachable: {exc}")
    with connect(CONFIG.sql, CONFIG.sql.test_database) as connection:
        yield connection


@pytest.fixture
def registry(tmp_path: Path) -> str:
    document = {
        "registry_version": "2026-10-18.1",
        "effective_date": "2026-10-18",
        "teams": [
            {
                "team_id": TEAM,
                "display_name": "Key Case Team",
                "v1_operators": ["team-op"],
                "v2_operator": "team-op-v2",
            }
        ],
    }
    path = tmp_path / "teams.json"
    path.write_text(json.dumps(document))
    return str(path)


def _execute(
    monkeypatch: pytest.MonkeyPatch, registry: str, db: Database, llm: FakeLlmClient
) -> PersistencePayload:
    rows = [
        v1_row(application=APPLICATION, key_field=key, **{"@timestamp": DAY})
        for key in sorted(KEYS)
    ]

    def fake_read(_client: Any, team: Any, window: Any) -> dict[str, SchemaAccumulator]:
        result = {}
        for schema, schema_rows in (("v1", rows), ("v2", [])):
            analysis = SchemaAccumulator(schema, window, team.panels_for(schema))
            for row in schema_rows:
                analysis.add(row)
            analysis.finish()
            result[schema] = analysis
        return result

    monkeypatch.setattr(orchestrator, "analyze_team", fake_read)
    payload, _summary = execute_run(
        team_id=TEAM,
        run_at=RUN_AT,
        config=CONFIG,
        es_client=object(),  # type: ignore[arg-type]
        llm_client=llm,
        registry_path=registry,
        db=db,
    )
    return payload


def _keys(db: Database, sql: str, params: dict[str, Any]) -> list[str]:
    return sorted(str(row["key_field"]) for row in db.query(sql, params))


def _run_id(db: Database) -> str:
    row = db.query_one("SELECT run_id FROM runs WHERE team_id=:team", {"team": TEAM})
    assert row is not None
    return str(row["run_id"])


def test_case_only_key_field_variants_persist_as_separate_identities(
    monkeypatch: pytest.MonkeyPatch, registry: str, db: Database
) -> None:
    llm = _model()
    payload = _execute(monkeypatch, registry, db, llm)
    run_id = str(payload.run["run_id"])
    assert {finding["key_field"] for finding in payload.findings} == KEYS
    assert {finding["application"] for finding in payload.findings} == {APPLICATION}
    assert llm.calls, "both identities need a fresh model verdict on the first run"

    persist_run(db, payload)

    expected = sorted(KEYS)
    run = {"run": run_id}
    assert _keys(db, "SELECT key_field FROM alert_findings WHERE run_id=:run", run) == expected
    assert _keys(db, "SELECT key_field FROM run_alert_list WHERE run_id=:run", run) == expected

    # Equality is exact: one spelling never matches the other.
    for key in KEYS:
        for table in ("alert_findings", "run_alert_list"):
            assert _keys(
                db,
                f"SELECT key_field FROM {table} "
                "WHERE run_id=:run AND application=:app AND key_field=:key",
                {"run": run_id, "app": APPLICATION, "key": key},
            ) == [key]

    verdicts = find_verdicts(
        db,
        str(payload.run["prompt_version"]),
        str(payload.run["model_version"]),
        [(APPLICATION, key) for key in sorted(KEYS)],
    )
    assert sorted(str(row["key_field"]) for row in verdicts.values()) == expected


def test_each_case_variant_reuses_only_its_own_stored_verdict(
    monkeypatch: pytest.MonkeyPatch, registry: str, db: Database
) -> None:
    # The previous test stored one verdict per spelling. The fake model gives every alert the
    # same verdict, so mark each stored row: a rerun that borrowed the other spelling's verdict
    # would carry the wrong marker.
    for key in KEYS:
        db.execute(
            "UPDATE llm_verdicts SET justification=:marker WHERE application=:app AND key_field=:key",
            {"marker": f"Stored verdict for {key}", "app": APPLICATION, "key": key},
        )
    db.commit()

    llm = _model()
    payload = _execute(monkeypatch, registry, db, llm)
    assert llm.calls == []
    justifications = {f["key_field"]: f["llm_justification"] for f in payload.findings}
    assert justifications == {key: f"Stored verdict for {key}" for key in KEYS}
    persist_run(db, payload)


def test_readers_and_decisions_tell_case_variants_apart(db: Database) -> None:
    # Runs last: a published run can no longer be replaced by a rerun.
    run_id = _run_id(db)
    publish_run(db, run_id)

    for key in sorted(KEYS):
        finding = db.query_one(
            "SELECT core_rule_ids, readiness_rule_ids, quality_state, llm_principle_id "
            "FROM alert_findings WHERE run_id=:run AND application=:app AND key_field=:key",
            {"run": run_id, "app": APPLICATION, "key": key},
        )
        assert finding is not None
        record_decision(
            db,
            run_id=run_id,
            alert_schema="v1",
            application=APPLICATION,
            key_field=key,
            finding_id=findings_on(finding)[-1],
            state="confirmed",
            note=f"decided on {key}",
        )

    for key in KEYS:
        exact = {"run": run_id, "app": APPLICATION, "key": key}
        for view in ("portal_alerts", "portal_alert_list"):
            assert _keys(
                db,
                f"SELECT key_field FROM {view} "
                "WHERE run_id=:run AND application=:app AND key_field=:key",
                exact,
            ) == [key]
        decisions = db.query(
            "SELECT key_field, note FROM portal_decisions "
            "WHERE team_id=:team AND application=:app AND key_field=:key",
            {"team": TEAM, "app": APPLICATION, "key": key},
        )
        assert [(row["key_field"], row["note"]) for row in decisions] == [
            (key, f"decided on {key}")
        ]


def test_every_stored_key_field_column_compares_exactly(db: Database) -> None:
    rows = db.query(
        "SELECT OBJECT_NAME(object_id) AS owner, collation_name FROM sys.columns "
        "WHERE name = N'key_field'"
    )
    collations = {str(row["owner"]): row["collation_name"] for row in rows}
    assert set(KEY_FIELD_TABLES + KEY_FIELD_VIEWS) <= set(collations)
    # A later migration must not silently restore a case-insensitive collation.
    assert {
        owner: collation for owner, collation in collations.items() if collation != BINARY
    } == {}

    keyed = db.query(
        "SELECT OBJECT_NAME(i.object_id) AS owner, i.name AS index_name, i.is_primary_key "
        "FROM sys.indexes AS i "
        "JOIN sys.index_columns AS ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
        "JOIN sys.columns AS c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
        "WHERE c.name = N'key_field' AND OBJECT_SCHEMA_NAME(i.object_id) = N'dbo'"
    )
    found = {(str(r["owner"]), str(r["index_name"])): bool(r["is_primary_key"]) for r in keyed}
    # Exactly these: an index on key_field that 016 did not rebuild would have blocked it.
    assert found == REBUILT_KEYS


def test_the_reader_role_reads_every_refreshed_view(db: Database) -> None:
    db.execute("IF USER_ID(N'key_case_reader') IS NULL CREATE USER key_case_reader WITHOUT LOGIN")
    db.execute("ALTER ROLE alerts_bi_reader ADD MEMBER key_case_reader")
    db.commit()
    db.execute("EXECUTE AS USER = N'key_case_reader'")
    try:
        for view in KEY_FIELD_VIEWS:
            db.query(f"SELECT TOP 1 key_field FROM {view}")
    finally:
        db.execute("REVERT")
