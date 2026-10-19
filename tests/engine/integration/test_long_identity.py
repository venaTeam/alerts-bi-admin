"""Every identity the columns can hold is stored, reused, published and decided on.

pk_alert_findings and pk_llm_verdicts were clustered keys over application and key_field,
which can be far wider than SQL Server's 900-byte clustered key limit. A team with an
identity whose application and key_field together exceeded 384 characters failed the whole
run with error 1946 (migration 018).
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
from src.engine.db.repositories import (
    PersistencePayload,
    find_verdicts,
    insert_verdict_if_absent,
    persist_run,
)
from src.engine.llm.fake import FakeLlmClient, scripted_verdicts
from src.engine.run import orchestrator
from src.engine.run.orchestrator import execute_run
from src.engine.run.streaming import SchemaAccumulator

from tests.engine.helpers.rows import v1_row
from tests.engine.helpers.sql import sample_verdict

pytestmark = pytest.mark.integration

CONFIG = load_config()
RUN_AT = datetime(2026, 8, 25, 18, 0, 0, tzinfo=UTC)
DAY = "2026-08-20T12:00:00.000Z"
TEAM = "long-identity-team"
# Both columns at their full width, with non-ASCII text: a lookup that hashed a differently
# encoded parameter would miss the stored verdict.
APPLICATION = ("long-app-é-" * 30)[:256]
PREFIX = ("disk:nöde-1/" * 50)[:511]
# The keys differ only in their last character, so nothing may compare a truncated prefix.
KEYS = {PREFIX + "a", PREFIX + "b"}
CLUSTERED_LIMIT = 900
NONCLUSTERED_LIMIT = 1700


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
        "registry_version": "2026-10-19.1",
        "effective_date": "2026-10-19",
        "teams": [
            {
                "team_id": TEAM,
                "display_name": "Long Identity Team",
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


def test_maximum_width_identities_persist(
    monkeypatch: pytest.MonkeyPatch, registry: str, db: Database
) -> None:
    llm = _model()
    payload = _execute(monkeypatch, registry, db, llm)
    run_id = str(payload.run["run_id"])
    assert {finding["key_field"] for finding in payload.findings} == KEYS
    assert llm.calls, "both identities need a fresh model verdict on the first run"

    persist_run(db, payload)

    expected = sorted(KEYS)
    for table in ("alert_findings", "run_alert_list"):
        assert (
            _keys(
                db,
                f"SELECT key_field FROM {table} WHERE run_id=:run AND application=:app",
                {"run": run_id, "app": APPLICATION},
            )
            == expected
        )
    verdicts = find_verdicts(
        db,
        str(payload.run["prompt_version"]),
        str(payload.run["model_version"]),
        [(APPLICATION, key) for key in expected],
    )
    assert sorted(str(row["key_field"]) for row in verdicts.values()) == expected
    assert {str(row["application"]) for row in verdicts.values()} == {APPLICATION}


def test_a_rerun_reuses_each_identity_s_own_verdict(
    monkeypatch: pytest.MonkeyPatch, registry: str, db: Database
) -> None:
    # The fake model gives every alert the same verdict, so mark each stored row: a rerun that
    # missed a stored verdict would call the model, and one that borrowed the other identity's
    # verdict would carry the wrong marker.
    for key in KEYS:
        db.execute(
            "UPDATE llm_verdicts SET justification=:marker WHERE application=:app AND key_field=:key",
            {"marker": f"Stored verdict ending {key[-1]}", "app": APPLICATION, "key": key},
        )
    db.commit()

    llm = _model()
    payload = _execute(monkeypatch, registry, db, llm)
    assert llm.calls == []
    justifications = {f["key_field"]: f["llm_justification"] for f in payload.findings}
    assert justifications == {key: f"Stored verdict ending {key[-1]}" for key in KEYS}
    persist_run(db, payload)


def test_readers_and_decisions_reach_each_identity(db: Database) -> None:
    # Runs after the reruns: a published run can no longer be replaced.
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
            note=f"decided on the key ending {key[-1]}",
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
            (key, f"decided on the key ending {key[-1]}")
        ]


def test_the_hashed_key_compares_identities_as_the_old_key_did(db: Database) -> None:
    # Binary collation: case matters. SQL Server padding: trailing spaces do not. The hashed
    # key accepts and rejects exactly these pairs, as the column key did before 018.
    for key in ("cmp:node-1", "cmp:Node-1", "cmp:node-1  "):
        insert_verdict_if_absent(db, sample_verdict(application="cmp-app", key_field=key))
    insert_verdict_if_absent(db, sample_verdict(application="cmp-app ", key_field="cmp:node-1"))
    db.commit()

    stored = db.query(
        "SELECT key_field FROM llm_verdicts WHERE application=N'cmp-app' ORDER BY key_field"
    )
    assert [row["key_field"] for row in stored] == ["cmp:Node-1", "cmp:node-1"]
    found = find_verdicts(
        db, "1.0.0", "fake-model-1", [("cmp-app", "cmp:node-1"), ("cmp-app", "CMP:NODE-1")]
    )
    assert sorted(str(row["key_field"]) for row in found.values()) == ["cmp:node-1"]


def test_every_key_fits_its_size_limit(db: Database) -> None:
    # SQL Server creates an oversized key with only a warning and fails the insert that first
    # exceeds it, so check every key's widest possible entry.
    rows = db.query(
        "SELECT OBJECT_NAME(i.object_id) AS owner, i.name AS index_name, i.type_desc, "
        "SUM(c.max_length) AS widest "
        "FROM sys.indexes AS i "
        "JOIN sys.index_columns AS ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
        "JOIN sys.columns AS c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
        "WHERE ic.is_included_column = 0 AND OBJECTPROPERTY(i.object_id, 'IsUserTable') = 1 "
        "AND OBJECT_SCHEMA_NAME(i.object_id) = N'dbo' "
        "GROUP BY i.object_id, i.name, i.type_desc"
    )
    assert rows
    limits = {"CLUSTERED": CLUSTERED_LIMIT, "NONCLUSTERED": NONCLUSTERED_LIMIT}
    oversized = {
        f"{row['owner']}.{row['index_name']}": int(row["widest"])
        for row in rows
        if int(row["widest"]) > limits[str(row["type_desc"])]
    }
    assert oversized == {}
