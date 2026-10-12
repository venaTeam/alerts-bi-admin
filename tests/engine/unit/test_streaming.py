"""Compare incremental facts against the materialized path on existing fixtures."""

from __future__ import annotations

import json
import random
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alerts_bi_operations.registry import Panel, load_registry, select_team
from alerts_bi_shared.window import build_run_window
from scripts.acceptance_teams import acceptance_teams
from scripts.generate_mock_alerts import NOW, expand_v1, expand_v2
from src.engine.domain.metrics import compute_daily_volume
from src.engine.domain.normalize import AlertRecord, normalize_row
from src.engine.rules.engine import (
    attach_row_findings,
    compute_daily_flagged,
    compute_daily_rule_counts,
    evaluate_rows,
)
from src.engine.rules.firing import firing_facts
from src.engine.rules.firing_stream import FiringAccumulator
from src.engine.run.orchestrator import _build_finding_row
from src.engine.run.streaming import SchemaAccumulator
from src.engine.suppression.evaluate import build_r5_findings, evaluate_suppression

from tests.engine.helpers.rows import v1_row, v2_row

WINDOW = build_run_window(NOW)
TEAMS = json.loads(Path("scripts/mock_teams.json").read_bytes())["teams"] + acceptance_teams()


def assert_parity(rows: list[AlertRecord], schema: str, panels: tuple[Panel, ...]) -> None:
    rows.sort(key=lambda r: r.timestamp)
    stream = SchemaAccumulator(schema, WINDOW, panels)
    for row in rows:
        stream.add(normalize_row(schema, row.source, hash_document=False))
    stream.finish()
    old = evaluate_rows(rows, NOW)
    visibility = evaluate_suppression(rows, panels)
    attach_row_findings(old, build_r5_findings(visibility.suppressed_row_ids, panels))
    assert stream.daily_volume() == compute_daily_volume(rows, WINDOW)
    assert stream.daily_flagged() == compute_daily_flagged(old.rows, WINDOW.snapshot_dates)
    assert stream.daily_rules() == compute_daily_rule_counts(old.rows, WINDOW.snapshot_dates)
    assert stream.suppression.interpretations == visibility.interpretations
    assert stream.suppression.notes == visibility.notes
    assert stream.suppression.unmeasured_leaves == visibility.unmeasured_leaves
    assert stream.suppression.unseen_unmeasured == visibility.unseen_unmeasured
    for field, ids in (
        ("suppressed", visibility.suppressed_row_ids),
        ("unseen", visibility.unseen_row_ids or set()),
    ):
        expected = dict.fromkeys(WINDOW.snapshot_dates, 0)
        for row in rows:
            expected[row.snapshot_date] += id(row) in ids
        assert stream.daily_visibility(field) == expected
    assert stream.identities.keys() == old.identities.keys()
    for key, identity in old.identities.items():
        assert _build_finding_row("r", stream.identities[key], None, None) == _build_finding_row(
            "r", identity, None, visibility.unseen_row_ids
        )


@pytest.mark.parametrize("team", TEAMS, ids=lambda t: t["name"])
@pytest.mark.parametrize("schema", ["v1", "v2"])
def test_existing_fixture_parity(team: dict[str, Any], schema: str) -> None:
    expand = expand_v1 if schema == "v1" else expand_v2
    rows = [
        normalize_row(schema, item["doc"])
        for definition in team.get(schema + "Defs", [])
        for item in expand(team, definition)
    ]
    rows = [r for r in rows if WINDOW.contains(r.timestamp)]
    panels = select_team(load_registry(), team["name"]).panels_for(schema)
    assert_parity(rows, schema, tuple(panels))


@pytest.mark.parametrize("schema", ["v1", "v2"])
def test_mixed_history_ties_provider_changes_and_suppression(schema: str) -> None:
    rng = random.Random(41)
    builder = v1_row if schema == "v1" else v2_row
    rows = [
        builder(
            **{
                "key_field": str(rng.randrange(5)),
                "@timestamp": (
                    WINDOW.window_start + timedelta(hours=rng.randrange(168))
                ).isoformat(),
                "time_created": "invalid" if rng.randrange(5) == 0 else None,
                "severity": rng.choice([1, 3, 5]),
                "status": rng.choice(["firing", "resolved"]),
                "provider": rng.choice(["grafana", "api"]),
                "message": rng.choice(["error occurred", "Disk full", "healthy"]),
                "node_name": rng.choice([None, "", "keep", "hide", "test"]),
                "impact": rng.choice([None, "high cpu", "Checkout is unavailable"]),
            }
        )
        for _ in range(700)
    ]
    panels = (
        Panel("p", schema, "SELECT * FROM t WHERE node_name != 'hide' AND node_name = 'keep'", ()),
    )
    assert_parity(rows, schema, panels)


def test_firing_facts_all_tie_orders_match_materialized_rule() -> None:
    rng = random.Random(23)
    for _ in range(50):
        rows = [
            v1_row(
                **{
                    "@timestamp": (
                        WINDOW.window_start + timedelta(hours=rng.randrange(100))
                    ).isoformat(),
                    "severity": rng.choice([1, 5]),
                }
            )
            for _ in range(100)
        ]
        rows.sort(key=lambda r: r.timestamp)
        for provider in ("grafana", "api"):
            stream = FiringAccumulator()
            for row in rows:
                stream.add(row)
            assert stream.finish(provider) == firing_facts("v1", rows, provider)


def test_repeated_events_do_not_accumulate_documents_or_signatures() -> None:
    stream = SchemaAccumulator("v1", WINDOW, ())
    for index in range(20000):
        stream.add(
            v1_row(**{"@timestamp": (WINDOW.window_start + timedelta(seconds=index)).isoformat()})
        )
    identity = next(iter(stream.identities.values()))
    assert len(stream.identities) == 1
    assert sum(len(day.signatures) for day in identity.days.values()) == 1
    assert len(identity.firing.cycles) == 0
    assert identity.row_count == 20000
    stream.finish()
    assert all(not day.signatures for day in identity.days.values())


def test_out_of_order_input_is_rejected() -> None:
    stream = SchemaAccumulator("v1", WINDOW, ())
    stream.add(v1_row(**{"@timestamp": "2026-08-21T12:00:00Z"}))
    with pytest.raises(ValueError, match="chronological"):
        stream.add(v1_row(**{"@timestamp": "2026-08-20T12:00:00Z"}))


def test_only_final_representative_needs_a_hash_without_timestamp_ties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alerts_bi_shared.hashing import sha256_of
    from src.engine.run import streaming

    calls = 0

    def counted(source: Any) -> str:
        nonlocal calls
        calls += 1
        return sha256_of(source)

    monkeypatch.setattr(streaming, "sha256_of", counted)
    stream = SchemaAccumulator("v1", WINDOW, ())
    source = v1_row().source
    for index in range(100):
        doc = {**source, "@timestamp": (WINDOW.window_start + timedelta(seconds=index)).isoformat()}
        stream.add(normalize_row("v1", doc, hash_document=False))
    assert calls == 0
    stream.finish()
    assert calls == 1
    representative = next(iter(stream.identities.values())).representative
    assert representative.doc_hash == sha256_of(representative.source)
