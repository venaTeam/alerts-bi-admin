"""Persistence against a real, disposable SQL Server database."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from alerts_bi_shared.db.connection import Database, connect, quote_identifier
from sqlalchemy.exc import DatabaseError
from src.engine.config import load_config
from src.engine.db.migrate import reset_test_database
from src.engine.db.repositories import (
    PersistencePayload,
    find_panel_parse,
    find_verdicts,
    get_batch_attempts,
    get_daily_metrics,
    get_findings,
    get_latest_run,
    get_rule_counts,
    get_run,
    persist_run,
    verdict_key,
)

from tests.engine.helpers.sql import (
    sample_daily,
    sample_finding,
    sample_run,
    sample_verdict,
)

pytestmark = pytest.mark.integration

CONFIG = load_config()


@pytest.fixture(scope="module")
def db() -> Iterator[Database]:
    try:
        reset_test_database(CONFIG.sql, CONFIG.sql.test_database)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"SQL Server is not reachable: {exc}")
    with connect(CONFIG.sql, CONFIG.sql.test_database) as connection:
        yield connection


def test_persists_a_run_and_reads_it_back(db: Database) -> None:
    run = sample_run()
    payload = PersistencePayload(
        run=run,
        daily_metrics=[sample_daily()],
        rule_counts=[
            {
                "run_id": run["run_id"],
                "team_id": run["team_id"],
                "alert_schema": "v1",
                "snapshot_date": "2026-08-20",
                "rule_id": "R2",
                "ruleset_version": "1.0.0",
                "match_count": 2,
                "distinct_count": 1,
            }
        ],
        findings=[sample_finding()],
    )
    persist_run(db, payload)

    stored = get_run(db, run["run_id"])
    assert stored is not None
    assert stored["team_id"] == "checkout-api"
    assert stored["phase_derived"] == "phase_1"
    assert float(stored["phase2_readiness_pct"]) == 50
    assert stored["llm_assessed"] is True
    assert len(get_daily_metrics(db, run["run_id"])) == 1
    assert len(get_rule_counts(db, run["run_id"])) == 1
    assert len(get_findings(db, run["run_id"])) == 1


def test_unseen_round_trips_as_null_and_as_an_integer(db: Database) -> None:
    run = sample_run(run_id="f" * 64)
    persist_run(
        db,
        PersistencePayload(
            run=run,
            daily_metrics=[
                sample_daily(run_id=run["run_id"], unseen=None, unseen_unmeasured=None),
                sample_daily(
                    run_id=run["run_id"],
                    alert_schema="v2",
                    unseen=4,
                    unseen_unmeasured=1,
                ),
            ],
            findings=[
                sample_finding(run_id=run["run_id"], unseen=None),
                sample_finding(run_id=run["run_id"], key_field="other-key", unseen=True),
                sample_finding(run_id=run["run_id"], key_field="third-key", unseen=False),
            ],
        ),
    )

    daily = {row["alert_schema"]: row for row in get_daily_metrics(db, run["run_id"])}
    assert daily["v1"]["unseen"] is None
    assert daily["v1"]["unseen_unmeasured"] is None
    assert daily["v2"]["unseen"] == 4
    assert daily["v2"]["unseen_unmeasured"] == 1

    findings = {row["key_field"]: row for row in get_findings(db, run["run_id"])}
    assert findings["checkout-api:cart:node-1"]["unseen"] is None
    assert findings["other-key"]["unseen"] is True
    assert findings["third-key"]["unseen"] is False


def test_re_persisting_the_same_run_id_replaces_its_rows(db: Database) -> None:
    run = sample_run(run_id="d" * 64)
    persist_run(db, PersistencePayload(run=run, daily_metrics=[sample_daily(run_id=run["run_id"])]))
    persist_run(
        db,
        PersistencePayload(run=run, daily_metrics=[sample_daily(run_id=run["run_id"], alerts=99)]),
    )

    daily = get_daily_metrics(db, run["run_id"])
    assert len(daily) == 1, "a rerun must not leave two generations of rows"
    assert daily[0]["alerts"] == 99


def test_persistence_is_atomic_a_bad_child_row_leaves_no_run_behind(db: Database) -> None:
    run = sample_run(run_id="e" * 64)
    # Violates ck_daily_metrics_distinct: distinct_alerts must not exceed alerts.
    with pytest.raises(DatabaseError, match="ck_daily_metrics_distinct|CHECK constraint"):
        persist_run(
            db,
            PersistencePayload(
                run=run,
                daily_metrics=[sample_daily(run_id=run["run_id"], alerts=1, distinct_alerts=5)],
            ),
        )
    assert get_run(db, run["run_id"]) is None


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"alerts": 2, "distinct_alerts": 3}, "ck_daily_metrics_distinct"),
        ({"flagged_by_rule": 1, "suppressed": 2}, "ck_daily_metrics_suppressed"),
    ],
)
def test_the_store_rejects_impossible_daily_metrics(
    db: Database, overrides: dict[str, Any], constraint: str
) -> None:
    run = sample_run(run_id="f" * 64)
    with pytest.raises(DatabaseError, match=f"{constraint}|CHECK constraint"):
        persist_run(
            db,
            PersistencePayload(
                run=run, daily_metrics=[sample_daily(run_id=run["run_id"], **overrides)]
            ),
        )


def test_the_store_rejects_an_unassessed_identity_with_no_reason(db: Database) -> None:
    run = sample_run(run_id="1" * 64)
    with pytest.raises(DatabaseError, match="ck_alert_findings_unassessed|CHECK constraint"):
        persist_run(
            db,
            PersistencePayload(
                run=run,
                findings=[
                    sample_finding(
                        run_id=run["run_id"], quality_state="unassessed", unassessed_reason=None
                    )
                ],
            ),
        )


def test_the_store_rejects_an_invalid_assessment_principle_pairing(db: Database) -> None:
    run = sample_run(run_id="2" * 64)
    with pytest.raises(DatabaseError, match="ck_llm_verdicts_pairing|CHECK constraint"):
        # no_violation must carry principle NONE, never a catalogue id.
        persist_run(
            db,
            PersistencePayload(
                run=run,
                verdicts=[sample_verdict(assessment="no_violation", principle_id="P1")],
            ),
        )


def _attempt(run_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "batch_id": "b" * 64,
        "attempt_number": 1,
        "group_type": "application",
        "group_value": "app",
        "partition_index": 0,
        "partition_count": 1,
        "alert_count": 1,
        "alert_ids": "[]",
        "request_hash": "h" * 64,
        "request_payload": "{}",
        "status": "succeeded",
        "failure_reason": None,
        "duration_ms": 10,
        "created_at": datetime(2026, 8, 25, 18, 0, 0),
        **overrides,
    }


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"attempt_number": 4}, "ck_llm_batch_attempts_number"),
        ({"alert_count": 201}, "ck_llm_batch_attempts_size"),
    ],
)
def test_the_store_rejects_an_impossible_batch_attempt(
    db: Database, overrides: dict[str, Any], constraint: str
) -> None:
    run = sample_run(run_id="3" * 64)
    with pytest.raises(DatabaseError, match=f"{constraint}|CHECK constraint"):
        persist_run(
            db,
            PersistencePayload(run=run, batch_attempts=[_attempt(run["run_id"], **overrides)]),
        )


def test_a_durable_verdict_survives_a_rerun_and_is_never_overwritten(db: Database) -> None:
    first = sample_run(run_id="5" * 64)
    persist_run(
        db,
        PersistencePayload(run=first, verdicts=[sample_verdict(first_run_id=first["run_id"])]),
    )

    # A second run tries to store a different verdict under the same cache key.
    second = sample_run(run_id="6" * 64)
    persist_run(
        db,
        PersistencePayload(
            run=second,
            verdicts=[
                sample_verdict(
                    first_run_id=second["run_id"],
                    assessment="catalog_violation",
                    principle_id="P2",
                    justification="changed my mind",
                )
            ],
        ),
    )

    found = find_verdicts(
        db, "1.0.0", "fake-model-1", [("checkout-api", "checkout-api:cart:node-1")]
    )
    verdict = found[verdict_key("checkout-api", "checkout-api:cart:node-1")]
    assert verdict["assessment"] == "no_violation", "the first stored verdict must win"
    assert verdict["principle_id"] == "NONE"
    assert verdict["first_run_id"] == "5" * 64, "verdicts outlive the run that produced them"


def test_verdict_lookup_is_scoped_to_the_prompt_and_model_version(db: Database) -> None:
    keys = [("checkout-api", "checkout-api:cart:node-1")]
    assert len(find_verdicts(db, "1.0.0", "fake-model-1", keys)) == 1
    assert find_verdicts(db, "2.0.0", "fake-model-1", keys) == {}, "a version bump must not reuse"
    assert find_verdicts(db, "1.0.0", "other-model", keys) == {}


def test_a_panel_parse_is_frozen_by_sql_text_hash_and_parser_version(db: Database) -> None:
    parse = {
        "sql_text_hash": "9" * 64,
        "parser_version": "1.0.0",
        "parsed_result": '{"leaves":1}',
        "safety_state": "parsed",
        "unmeasured_reason": None,
        "created_at": datetime(2026, 8, 25, 18, 0, 0),
    }
    persist_run(db, PersistencePayload(run=sample_run(run_id="7" * 64), panel_parses=[parse]))
    persist_run(
        db,
        PersistencePayload(
            run=sample_run(run_id="8" * 64),
            panel_parses=[{**parse, "parsed_result": '{"leaves":999}'}],
        ),
    )

    stored = find_panel_parse(db, "9" * 64, "1.0.0")
    assert stored is not None
    assert stored["parsed_result"] == '{"leaves":1}', "identical SQL keeps its interpretation"
    assert find_panel_parse(db, "9" * 64, "2.0.0") is None


def test_batch_attempts_read_back_in_batch_and_attempt_order(db: Database) -> None:
    run = sample_run(run_id="c" * 64)
    base = {
        "group_type": "alert_rule_url",
        "group_value": "https://grafana.internal/d/x",
        "alert_count": 2,
        "alert_ids": '["a","b"]',
    }
    persist_run(
        db,
        PersistencePayload(
            run=run,
            batch_attempts=[
                _attempt(run["run_id"], batch_id="b2".ljust(64, "0"), attempt_number=1, **base),
                _attempt(run["run_id"], batch_id="b1".ljust(64, "0"), attempt_number=2, **base),
                _attempt(run["run_id"], batch_id="b1".ljust(64, "0"), attempt_number=1, **base),
            ],
        ),
    )
    attempts = get_batch_attempts(db, run["run_id"])
    assert [f"{a['batch_id'][:2]}#{a['attempt_number']}" for a in attempts] == [
        "b1#1",
        "b1#2",
        "b2#1",
    ]


def test_get_latest_run_returns_the_most_recent_completed_run(db: Database) -> None:
    latest = get_latest_run(db, "checkout-api")
    assert latest is not None
    assert latest["team_id"] == "checkout-api"


def _at(text: str) -> datetime:
    """SQL Server DATETIME2 columns take naive UTC values through this driver."""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)


def test_runs_tied_on_run_at_are_broken_by_when_they_actually_finished(db: Database) -> None:
    """A team re-run over the same frozen clock produces distinct runs with equal run_at.

    Ordering on run_at alone leaves them tied, and a tie resolves to whichever row the
    engine happens to return - which made "latest" silently mean "oldest".
    """
    team = "tie-break-team"
    for index, (suffix, finished) in enumerate(
        [
            ("a", "2026-08-30T08:00:00Z"),
            ("b", "2026-08-30T13:00:00Z"),
            ("c", "2026-08-30T11:00:00Z"),
        ]
    ):
        persist_run(
            db,
            PersistencePayload(
                run=sample_run(
                    run_id=f"{index}{suffix}" + "0" * 62,
                    team_id=team,
                    run_at=_at("2026-08-25T18:00:00Z"),
                    completed_at=_at(finished),
                ),
                daily_metrics=[],
                rule_counts=[],
                findings=[],
            ),
        )

    latest = get_latest_run(db, team)
    assert latest is not None
    assert latest["run_id"].startswith("1b"), "the run that finished last, not the first row"


def test_reset_refuses_any_database_that_is_not_the_configured_test_one() -> None:
    for database in ("alerts_bi_dev", "master"):
        with pytest.raises(ValueError, match="only the configured test database"):
            reset_test_database(CONFIG.sql, database)


def test_sql_identifiers_are_validated_before_reaching_ddl() -> None:
    assert quote_identifier("alerts_bi_test") == "[alerts_bi_test]"
    for unsafe in ("alerts_bi_test]; DROP DATABASE x --", "has space", "", "1leading_digit"):
        with pytest.raises(ValueError, match="unsafe SQL identifier"):
            quote_identifier(unsafe)
