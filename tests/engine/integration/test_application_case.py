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
from alerts_bi_shared.db.connection import Database, connect
from src.engine.config import load_config
from src.engine.db.migrate import reset_test_database
from src.engine.db.repositories import PersistencePayload, find_verdicts, persist_run
from src.engine.llm.fake import FakeLlmClient
from src.engine.run import orchestrator
from src.engine.run.orchestrator import execute_run
from src.engine.run.streaming import SchemaAccumulator

from tests.engine.helpers.rows import v1_row

pytestmark = pytest.mark.integration

CONFIG = load_config()
RUN_AT = datetime(2026, 8, 25, 18, 0, 0, tzinfo=UTC)
DAY = "2026-08-20T12:00:00.000Z"
# One key_field for both, so even alert_findings' (application, key_field) key is shared.
KEY = "disk:node-1"
APPLICATIONS = {"Moby", "moby"}


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
    monkeypatch: pytest.MonkeyPatch, registry: str, db: Database, llm: FakeLlmClient
) -> PersistencePayload:
    rows = [
        v1_row(application=application, key_field=KEY, **{"@timestamp": DAY})
        for application in sorted(APPLICATIONS)
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
        team_id="case-team",
        run_at=RUN_AT,
        config=CONFIG,
        es_client=object(),  # type: ignore[arg-type]
        llm_client=llm,
        registry_path=registry,
        db=db,
    )
    return payload


def _applications(db: Database, sql: str, run_id: str) -> list[str]:
    return sorted(str(row["application"]) for row in db.query(sql, {"run": run_id}))


def test_case_only_application_variants_persist_as_separate_identities(
    monkeypatch: pytest.MonkeyPatch, registry: str, db: Database
) -> None:
    llm = FakeLlmClient()
    payload = _execute(monkeypatch, registry, db, llm)
    run_id = str(payload.run["run_id"])
    assert {finding["application"] for finding in payload.findings} == APPLICATIONS
    assert llm.calls, "both identities need a fresh model verdict on the first run"

    persist_run(db, payload)

    expected = sorted(APPLICATIONS)
    assert (
        _applications(db, "SELECT application FROM alert_findings WHERE run_id=:run", run_id)
        == expected
    )
    assert (
        _applications(db, "SELECT application FROM run_alert_list WHERE run_id=:run", run_id)
        == expected
    )
    assert (
        _applications(
            db,
            "SELECT application FROM run_summaries WHERE run_id=:run AND scope_kind='application'",
            run_id,
        )
        == expected
    )
    # Every window day has a row per application; both alerted on the same day.
    assert (
        _applications(
            db,
            "SELECT application FROM run_application_daily "
            "WHERE run_id=:run AND snapshot_date='2026-08-20' AND events > 0",
            run_id,
        )
        == expected
    )
    assert (
        _applications(db, "SELECT application FROM run_v1_rules WHERE run_id=:run", run_id)
        == expected
    )

    # Equality is exact: one spelling never matches the other.
    for application in APPLICATIONS:
        rows = db.query(
            "SELECT application FROM alert_findings WHERE run_id=:run AND application=:app",
            {"run": run_id, "app": application},
        )
        assert [row["application"] for row in rows] == [application]

    verdicts = find_verdicts(
        db,
        str(payload.run["prompt_version"]),
        str(payload.run["model_version"]),
        [(application, KEY) for application in sorted(APPLICATIONS)],
    )
    assert sorted(str(row["application"]) for row in verdicts.values()) == expected


def test_each_case_variant_reuses_only_its_own_stored_verdict(
    monkeypatch: pytest.MonkeyPatch, registry: str, db: Database
) -> None:
    # The previous test stored one verdict per spelling; a rerun needs no model call.
    llm = FakeLlmClient()
    payload = _execute(monkeypatch, registry, db, llm)
    assert llm.calls == []
    assert {finding["application"] for finding in payload.findings} == APPLICATIONS
    persist_run(db, payload)
