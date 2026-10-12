"""End-to-end pipeline over the real mock Elasticsearch and a disposable SQL Server database.

This is the vertical slice the blueprint's milestone 2 asks for, extended with suppression,
LLM assessment and reporting.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alerts_bi_operations.report.render import OUTPUT_FILES, render_run_report
from alerts_bi_shared.db.connection import Database, connect
from src.engine.config import load_config
from src.engine.db.migrate import reset_test_database
from src.engine.db.repositories import (
    get_daily_metrics,
    get_findings,
    get_run,
    persist_run,
)
from src.engine.es.client import EsClient
from src.engine.es.reader import V1_INDEX
from src.engine.llm.fake import FakeLlmClient
from src.engine.run.orchestrator import RunSummary, execute_run

pytestmark = pytest.mark.integration

CONFIG = load_config()

#: The mock dataset is generated against this fixed clock.
RUN_AT = datetime(2026, 8, 25, 18, 0, 0, tzinfo=UTC)
OUT_DIR = Path("out") / "test-pipeline"

RunFn = Callable[..., tuple[Any, RunSummary]]


@pytest.fixture(scope="module")
def es_client() -> EsClient:
    client = EsClient(CONFIG.es)
    try:
        available = client.index_exists(V1_INDEX)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"mock Elasticsearch is not reachable: {exc}")
    if not available:
        pytest.skip(
            "mock Elasticsearch has no appchi-v1 index; run: docker compose up -d && "
            "RESET=1 uv run python scripts/generate_mock_alerts.py"
        )
    return client


@pytest.fixture(scope="module")
def db() -> Iterator[Database]:
    try:
        reset_test_database(CONFIG.sql, CONFIG.sql.test_database)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"SQL Server is not reachable: {exc}")
    with connect(CONFIG.sql, CONFIG.sql.test_database) as connection:
        yield connection


@pytest.fixture(scope="module")
def run(es_client: EsClient, db: Database) -> Iterator[RunFn]:
    """Execute one run and persist it, the way the CLI does."""

    def _run(
        team_id: str,
        *,
        llm_client: Any = _UNSET,
        llm_disabled_reason: str | None = None,
        reuse_db: Database | None = None,
    ) -> tuple[Any, RunSummary]:
        payload, summary = execute_run(
            team_id=team_id,
            run_at=RUN_AT,
            config=CONFIG,
            es_client=es_client,
            # An explicit None must mean "no model"; a plain default would quietly hand the
            # run a fake client instead.
            llm_client=FakeLlmClient() if llm_client is _UNSET else llm_client,
            llm_disabled_reason=llm_disabled_reason,
            db=reuse_db,
        )
        persist_run(db, payload)
        return payload, summary

    yield _run
    shutil.rmtree(OUT_DIR, ignore_errors=True)


class _Unset:
    pass


_UNSET = _Unset()


def test_a_full_run_persists_and_reconciles_with_its_source_rows(run: RunFn, db: Database) -> None:
    payload, summary = run("checkout-api")

    stored = get_run(db, summary.run_id)
    assert stored is not None
    assert stored["team_id"] == "checkout-api"
    assert stored["status"] == "completed"
    assert stored["llm_assessed"] is True

    daily = get_daily_metrics(db, summary.run_id)
    # Eight UTC dates times two schemas.
    assert len(daily) == 16

    v1_rows = sum(d["alerts"] for d in daily if d["alert_schema"] == "v1")
    assert v1_rows == summary.v1_rows, "stored daily rows must sum to the rows actually read"

    covered = sum(float(d["covered_hours"]) for d in daily if d["alert_schema"] == "v1")
    assert covered == 168

    assert len(payload.findings) == summary.v1_identities + summary.v2_identities


def test_re_running_the_same_team_and_clock_is_idempotent(run: RunFn, db: Database) -> None:
    _, first = run("checkout-api")
    _, second = run("checkout-api")
    assert first.run_id == second.run_id, "a frozen run_at gives a stable run id"
    assert len(get_daily_metrics(db, second.run_id)) == 16


def test_every_identity_carries_exactly_one_of_the_five_states(run: RunFn, db: Database) -> None:
    _, summary = run("data-pipeline-etl")
    findings = get_findings(db, summary.run_id)
    allowed = {"rule_flagged", "llm_flagged", "needs_review", "assessed_good", "unassessed"}
    assert findings
    for finding in findings:
        assert finding["quality_state"] in allowed, finding["quality_state"]

    # Identity is unique per run and schema.
    keys = [f"{f['alert_schema']}|{f['application']}|{f['key_field']}" for f in findings]
    assert len(set(keys)) == len(keys)


def test_an_identity_with_a_core_finding_is_never_sent_to_the_model(
    run: RunFn, db: Database
) -> None:
    _, summary = run("legacy-batch-jobs")
    for finding in get_findings(db, summary.run_id):
        if finding["core_rule_ids"]:
            assert finding["quality_state"] == "rule_flagged", (
                f"{finding['key_field']} has {finding['core_rule_ids']}"
            )
            assert finding["llm_principle_id"] is None


def test_a_v2_readiness_gap_does_not_prevent_assessment(run: RunFn, db: Database) -> None:
    _, summary = run("search-platform")
    gapped = [
        f
        for f in get_findings(db, summary.run_id)
        if f["readiness_rule_ids"] and not f["core_rule_ids"]
    ]
    for finding in gapped:
        assert finding["quality_state"] != "rule_flagged"
        assert finding["quality_state"] != "unassessed"


def test_running_without_a_model_marks_eligible_identities_unassessed_with_a_reason(
    run: RunFn, db: Database
) -> None:
    _, summary = run(
        "payments-core",  # a team whose alerts include model-eligible ones
        llm_client=None,
        llm_disabled_reason="LLM assessment was disabled for this run",
    )
    stored = get_run(db, summary.run_id)
    assert stored is not None
    assert stored["llm_assessed"] is False

    unassessed = [f for f in get_findings(db, summary.run_id) if f["quality_state"] == "unassessed"]
    assert unassessed
    for finding in unassessed:
        assert "disabled" in finding["unassessed_reason"]


def test_durable_verdicts_are_reused_on_a_second_run_issuing_no_new_requests(
    run: RunFn, db: Database
) -> None:
    first_client = FakeLlmClient()
    run("payments-core", llm_client=first_client, reuse_db=db)
    assert first_client.calls, "the first run should call the model"

    second_client = FakeLlmClient()
    run("payments-core", llm_client=second_client, reuse_db=db)
    assert second_client.calls == [], "the second run should reuse every stored verdict"


def test_suppression_findings_appear_as_core_rule_r5_and_are_counted_in_suppressed(
    run: RunFn, db: Database
) -> None:
    payload, summary = run("notifications-svc")
    findings = get_findings(db, summary.run_id)
    suppressed = [f for f in findings if "R5" in (f["core_rule_ids"] or "").split(",")]

    daily = get_daily_metrics(db, summary.run_id)
    suppressed_rows = sum(d["suppressed"] for d in daily)
    flagged_rows = sum(d["flagged_by_rule"] for d in daily)

    if suppressed:
        assert suppressed_rows > 0
        # suppressed is a SUBSET of flagged_by_rule, never an addition to it.
        assert suppressed_rows <= flagged_rows
    assert payload.run_panels, "supplied panels are published with the numbers"


def test_reports_render_from_sql_only_and_write_exactly_the_four_approved_files(
    run: RunFn, db: Database
) -> None:
    _, summary = run("checkout-api")
    files = render_run_report(db, summary.run_id, OUT_DIR)

    assert [f.name for f in files] == list(OUTPUT_FILES)
    for file in files:
        assert file.exists(), str(file)

    html = (OUT_DIR / "scorecard.html").read_text(encoding="utf-8")
    assert "Alerts BI scorecard" in html
    assert "Checkout API" in html

    lines = _csv_lines(OUT_DIR / "daily_metrics.csv")
    assert len(lines) - 1 == 16


def test_rendering_an_unknown_run_id_fails_rather_than_producing_an_empty_report(
    db: Database,
) -> None:
    with pytest.raises(Exception, match="not in the store"):
        render_run_report(db, "f" * 64, OUT_DIR)


def test_the_csv_daily_rows_reconcile_with_the_stored_rows_exactly(
    run: RunFn, db: Database
) -> None:
    _, summary = run("data-pipeline-etl")
    render_run_report(db, summary.run_id, OUT_DIR)
    daily = get_daily_metrics(db, summary.run_id)

    lines = _csv_lines(OUT_DIR / "daily_metrics.csv")
    alerts_index = lines[0].split(",").index("alerts")
    csv_total = sum(int(line.split(",")[alerts_index]) for line in lines[1:])
    assert csv_total == sum(d["alerts"] for d in daily)


def test_a_run_for_an_unknown_team_fails_before_any_elasticsearch_query(
    es_client: EsClient,
) -> None:
    with pytest.raises(Exception, match="is not in the registry"):
        execute_run(
            team_id="not-a-team",
            run_at=RUN_AT,
            config=CONFIG,
            es_client=es_client,
            llm_client=None,
            llm_disabled_reason="disabled",
        )


@pytest.mark.parametrize(
    "team_id",
    [
        "payments-core",
        "legacy-batch-jobs",
        "fraud-detection",
        "checkout-api",
        "notifications-svc",
        "search-platform",
        "data-pipeline-etl",
    ],
)
def test_every_registered_team_runs_end_to_end(run: RunFn, db: Database, team_id: str) -> None:
    _, summary = run(team_id)
    stored = get_run(db, summary.run_id)
    assert stored is not None
    assert stored["status"] == "completed", team_id
    assert stored["phase_derived"] in {"no_data", "phase_0", "phase_1", "phase_2", "done"}, (
        f"{team_id}: {stored['phase_derived']}"
    )


def _csv_lines(path: Path) -> list[str]:
    """Split on CRLF explicitly.

    The exports are RFC 4180 CRLF, and reading them as universal-newline text would hide a
    line-ending regression rather than surface it.
    """
    return path.read_bytes().decode("utf-8").strip().split("\r\n")
