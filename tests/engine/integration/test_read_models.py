"""Exact compact facts, publication boundaries, seek paging and pool isolation on SQL Server."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace

import pytest
from alerts_bi_shared.db.connection import Database, WebDatabasePool, connect
from alerts_bi_shared.insights.reader import read_summary
from alerts_bi_shared.insights.stored import alert_from_row, build_facts
from alerts_bi_shared.insights.summary import summarize
from src.engine.config import load_config
from src.engine.db.migrate import reset_test_database
from src.engine.db.repositories import PersistencePayload, persist_run
from src.summary import WorklistFilter, _worklist, summary_inputs

from tests.engine.helpers.sql import sample_daily, sample_finding, sample_run

pytestmark = pytest.mark.integration
CONFIG = load_config()


@pytest.fixture(scope="module")
def db() -> Iterator[Database]:
    reset_test_database(CONFIG.sql, CONFIG.sql.test_database)
    with connect(CONFIG.sql, CONFIG.sql.test_database) as connection:
        yield connection


def test_exact_totals_survive_bounded_examples_and_rule_filters(db: Database) -> None:
    run = sample_run()
    rows = [
        sample_finding(
            key_field=f"key-{i:04d}",
            application="app-a" if i % 2 else "app-b",
            row_count=100 if i < 5 else 1,
            quality_state="rule_flagged",
            core_rule_ids="R1,R6",
            fire_pattern="stuck",
            clear_count=0,
            max_clear_cycles_24h=0,
            max_episode_firing_rows=100,
            open_since=None,
            unseen=None,
            findings_evidence=json.dumps(
                [
                    {"rule_id": "R1", "matched_rows": 1},
                    {"rule_id": "R6", "matched_rows": 100 if i < 5 else 1},
                ]
            ),
        )
        for i in range(143)
    ]
    daily = sample_daily(alerts=sum(r["row_count"] for r in rows), distinct_alerts=len(rows))
    persist_run(db, PersistencePayload(run=run, findings=rows, daily_metrics=[daily]))
    facts, examples, _ = read_summary(db, run["run_id"], "admin")
    assert len(examples) <= 18 and facts["schemas"]["v1"]["distinct_alerts"] == 143
    reference = summary_inputs(
        run, [daily], [], tuple(alert_from_row(r) for r in rows), published=False, history=()
    )
    facts["rules_left"] = 1
    # Same complete aggregates with deliberately tiny example input.
    expected = summarize(reference)
    actual = summarize(replace(reference, alerts=examples, facts=facts))
    assert actual.by_application == expected.by_application
    assert actual.biggest == expected.biggest
    assert actual.key_findings == expected.key_findings
    assert actual.estimate == expected.estimate
    first, total, filters = _worklist(db, run["run_id"], WorklistFilter(rule="R1"))
    second, _, second_filters = _worklist(
        db, run["run_id"], WorklistFilter(rule="R1", page=2, cursor=filters.next_cursor)
    )
    previous, _, _ = _worklist(
        db, run["run_id"], WorklistFilter(rule="R1", page=1, cursor=second_filters.previous_cursor)
    )
    assert total == 143 and len(first) == len(second) == 50 and previous == first
    assert not (
        {(a.application, a.key_field) for a in first}
        & {(a.application, a.key_field) for a in second}
    )
    assert _worklist(db, run["run_id"], WorklistFilter(rule="R10"))[1] == 0
    appfacts, _, _ = read_summary(db, run["run_id"], "admin", ("app-a",))
    expected_app = build_facts([r for r in rows if r["application"] == "app-a"])
    for schema in ("v1", "v2"):
        appfacts["schemas"][schema].pop("concentration", None)
    assert appfacts == expected_app
    assert appfacts["schemas"]["v1"]["events"] == sum(
        r["row_count"] for r in rows if r["application"] == "app-a"
    )
    # No portal rows before publication, including compact summaries/examples.
    assert db.query("SELECT * FROM portal_run_summaries") == []
    assert db.query("SELECT TOP 1 * FROM portal_alert_list") == []


def test_old_readiness_evidence_is_not_current_membership(db: Database) -> None:
    run = sample_run(run_id="c" * 64)
    row = sample_finding(
        run_id=run["run_id"],
        alert_schema="v2",
        readiness_rule_ids="R10",
        findings_evidence='[{"rule_id":"R8","matched_rows":3},{"rule_id":"R10","matched_rows":2}]',
    )
    persist_run(db, PersistencePayload(run=run, findings=[row]))
    rules = db.query(
        "SELECT rule_id FROM run_rule_membership WHERE run_id=:run", {"run": run["run_id"]}
    )
    assert rules == [{"rule_id": "R10"}]


def test_pool_reuses_session_but_rolls_back_uncommitted_request(db: Database) -> None:
    pool = WebDatabasePool(CONFIG.sql, CONFIG.sql.test_database, pool_size=1, max_overflow=0)
    try:
        with pool.connect() as first:
            spid = first.query_one("SELECT @@SPID AS spid")
            first.execute(
                "UPDATE runs SET team_display_name='uncommitted' WHERE run_id=:run",
                {"run": "a" * 64},
            )
        with pool.connect() as second:
            assert second.query_one("SELECT @@SPID AS spid") == spid
            assert second.query_one(
                "SELECT team_display_name FROM runs WHERE run_id=:run", {"run": "a" * 64}
            ) == {"team_display_name": "Checkout API"}
    finally:
        pool.dispose()
