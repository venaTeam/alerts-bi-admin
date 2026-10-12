"""Orchestrator wiring of `unseen` (spec section 6): daily allocation and the work list."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from src.engine.config import load_config
from src.engine.run import orchestrator
from src.engine.run.orchestrator import execute_run
from src.engine.run.streaming import SchemaAccumulator

from tests.engine.helpers.rows import v1_row, v2_row

RUN_AT = datetime(2026, 8, 25, 18, 0, 0, tzinfo=UTC)
DAY_1 = "2026-08-20T12:00:00.000Z"
DAY_2 = "2026-08-21T12:00:00.000Z"


def _registry(tmp_path: Path, v1_sql: str | None, v2_sql: str | None) -> str:
    panels = []
    if v1_sql:
        panels.append({"panel_id": "p-v1", "schema": "v1", "sql": v1_sql})
    if v2_sql:
        panels.append({"panel_id": "p-v2", "schema": "v2", "sql": v2_sql})
    document = {
        "registry_version": "2026-10-01.1",
        "effective_date": "2026-10-01",
        "teams": [
            {
                "team_id": "t1",
                "display_name": "Team One",
                "v1_operators": ["team-op"],
                "v2_operator": "team-op-v2",
                "panels": panels,
            }
        ],
    }
    path = tmp_path / "teams.json"
    path.write_text(json.dumps(document))
    return str(path)


def _run(monkeypatch: pytest.MonkeyPatch, registry: str) -> Any:
    v1 = [
        v1_row(application="a", key_field="k1", **{"@timestamp": DAY_1}),
        v1_row(application="b", key_field="k2", **{"@timestamp": DAY_1}),
        v1_row(application="b", key_field="k2", **{"@timestamp": DAY_2}),
        v1_row(application="a", key_field="k1", **{"@timestamp": DAY_2}),
    ]
    v2 = [v2_row(application="b", key_field="k3", **{"@timestamp": DAY_1})]

    def fake_read(_client: Any, _team: Any, _window: Any) -> dict[str, SchemaAccumulator]:
        result = {}
        for schema, rows in (("v1", v1), ("v2", v2)):
            analysis = SchemaAccumulator(schema, _window, _team.panels_for(schema))
            for row in rows:
                analysis.add(row)
            analysis.finish()
            result[schema] = analysis
        return result

    monkeypatch.setattr(orchestrator, "analyze_team", fake_read)
    payload, _summary = execute_run(
        team_id="t1",
        run_at=RUN_AT,
        config=load_config(),
        es_client=object(),  # type: ignore[arg-type]
        llm_client=None,
        registry_path=registry,
    )
    return payload


def test_unseen_is_an_integer_with_a_panel_and_null_without_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The nested node_name leaf is an identity leaf under OR, so it is unmeasured once.
    registry = _registry(
        tmp_path,
        "SELECT * FROM t WHERE application = 'a' AND (node_name = 'x' OR severity = 'critical')",
        None,
    )
    payload = _run(monkeypatch, registry)

    v1 = sorted(
        (m for m in payload.daily_metrics if m["alert_schema"] == "v1"),
        key=lambda m: m["snapshot_date"],
    )
    v2 = [m for m in payload.daily_metrics if m["alert_schema"] == "v2"]

    by_date = {m["snapshot_date"].isoformat(): m for m in v1}
    assert by_date["2026-08-20"]["unseen"] == 1
    assert by_date["2026-08-21"]["unseen"] == 1
    assert sum(m["unseen"] for m in v1) == 2

    # Unmeasured sits on the schema's first bucket only.
    assert v1[0]["unseen_unmeasured"] == 1
    assert all(m["unseen_unmeasured"] == 0 for m in v1[1:])

    # A schema with no panel is NULL, never 0.
    assert v2
    assert all(m["unseen"] is None and m["unseen_unmeasured"] is None for m in v2)


def test_finding_unseen_is_none_true_or_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = _registry(tmp_path, "SELECT * FROM t WHERE application = 'a'", None)
    payload = _run(monkeypatch, registry)

    findings = {(f["alert_schema"], f["key_field"]): f["unseen"] for f in payload.findings}
    assert findings[("v1", "k1")] is False
    assert findings[("v1", "k2")] is True
    assert findings[("v2", "k3")] is None
