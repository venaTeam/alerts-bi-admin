"""Pre-call visibility and recovery against the disposable real SQL Server."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from alerts_bi_operations.review.publication import publish_run
from alerts_bi_shared.db.connection import Database, connect
from alerts_bi_shared.hashing import sha256_text
from sqlalchemy.exc import DBAPIError
from src.engine.config import load_config
from src.engine.db.llm_audit import SqlLlmJournal
from src.engine.db.migrate import reset_test_database
from src.engine.db.repositories import PersistencePayload, RunIsPublished, persist_run
from src.engine.llm.assess import AssessmentResult, assess_alerts
from src.engine.llm.fake import FakeLlmClient, ScriptedResult

from tests.engine.helpers.rows import v1_row
from tests.engine.helpers.sql import sample_run

pytestmark = pytest.mark.integration
CONFIG = load_config()


@pytest.fixture(scope="module")
def db() -> Iterator[Database]:
    assert CONFIG.sql.host in {"localhost", "127.0.0.1"}
    assert CONFIG.sql.test_database == "alerts_bi_test"
    reset_test_database(CONFIG.sql, CONFIG.sql.test_database)
    with connect(CONFIG.sql, CONFIG.sql.test_database) as connection:
        yield connection


def assess(db: Database, scope: str, client: FakeLlmClient, **kwargs: Any) -> AssessmentResult:
    return assess_alerts(
        alerts=[v1_row()],
        client=client,
        system_prompt="audit test prompt",
        run_id=scope,
        prompt_version="audit-test",
        model_version=client.model_version,
        now=datetime(2026, 8, 25, tzinfo=UTC),
        journal=SqlLlmJournal(db, scope),
        clock=lambda: datetime.now(UTC),
        **kwargs,
    )


def test_request_and_started_attempt_are_visible_before_the_call_and_replay_without_network(
    db: Database,
) -> None:
    def observe(request: dict[str, Any]) -> dict[str, Any]:
        with connect(CONFIG.sql, CONFIG.sql.test_database) as other:
            row = other.query_one(
                "SELECT b.request_payload,b.request_hash,a.status "
                "FROM llm_review_batches b JOIN llm_review_attempts a ON a.cycle_id=b.cycle_id "
                "WHERE b.scope_id=:scope",
                {"scope": "a" * 64},
            )
            assert row is not None and row["status"] == "started"
            assert sha256_text(row["request_payload"]) == row["request_hash"]
            assert (
                other.query_one(
                    "SELECT 1 AS found FROM runs WHERE run_id=:scope", {"scope": "a" * 64}
                )
                is None
            )
        return FakeLlmClient._build_default(request, ScriptedResult("ok"))

    client = FakeLlmClient(fallback=ScriptedResult("ok", build=observe))
    first = assess(db, "a" * 64, client)
    second = assess(db, "a" * 64, client)
    assert len(client.calls) == 1
    assert first.new_verdicts[0]["classified_at"].year == datetime.now(UTC).year
    assert second.new_verdicts[0]["principle_id"] == first.new_verdicts[0]["principle_id"]
    assert second.new_verdicts[0]["classified_at"] == first.new_verdicts[0]["classified_at"]
    assert second.batch_attempts[0]["request_payload"] == first.batch_attempts[0]["request_payload"]
    assert db.query_one("SELECT COUNT(*) AS n FROM llm_verdicts")["n"] == 0  # type: ignore[index]


def test_interrupted_call_consumes_an_attempt_on_recovery(db: Database) -> None:
    class Crash(BaseException):
        pass

    def crash(_request: dict[str, Any]) -> dict[str, Any]:
        raise Crash()

    with pytest.raises(Crash):
        assess(db, "b" * 64, FakeLlmClient(fallback=ScriptedResult("ok", build=crash)))
    resumed = FakeLlmClient()
    result = assess(db, "b" * 64, resumed)
    assert [a["status"] for a in result.batch_attempts] == ["interrupted", "succeeded"]
    assert [c.attempt for c in resumed.calls] == [2]
    assert len({a["request_payload"] for a in result.batch_attempts}) == 1


def three_rules() -> list[Any]:
    return [v1_row(key_field=f"k{n}", alert_rule_url=f"https://g/d/{n}") for n in range(3)]


def assess_rules(
    db: Database, scope: str, client: FakeLlmClient, concurrency: int
) -> AssessmentResult:
    return assess_alerts(
        alerts=three_rules(),
        client=client,
        system_prompt="audit test prompt",
        run_id=scope,
        prompt_version="audit-test",
        model_version=client.model_version,
        now=datetime(2026, 8, 25, tzinfo=UTC),
        journal=SqlLlmJournal(db, scope),
        clock=lambda: datetime.now(UTC),
        audit_settings={"model_deployment": "fake"},
        concurrency=concurrency,
    )


def test_parallel_batches_share_the_journal_and_replay_at_another_concurrency(
    db: Database,
) -> None:
    scope = "p" * 64
    first_client = FakeLlmClient()
    first = assess_rules(db, scope, first_client, 3)
    batch_ids = [a["batch_id"] for a in first.batch_attempts]
    assert len(set(batch_ids)) == 3 and len(first_client.calls) == 3
    rows = db.query(
        "SELECT a.status,COUNT(*) AS n FROM llm_review_batches b "
        "JOIN llm_review_attempts a ON a.cycle_id=b.cycle_id WHERE b.scope_id=:scope "
        "GROUP BY a.status",
        {"scope": scope},
    )
    assert rows == [{"status": "succeeded", "n": 3}]

    # The setting is not a recorded model setting: changing it replays, never refuses.
    replay_client = FakeLlmClient()
    replay = assess_rules(db, scope, replay_client, 1)
    assert replay_client.calls == []
    assert [a["batch_id"] for a in replay.batch_attempts] == batch_ids
    assert replay.new_verdicts == first.new_verdicts


def test_several_interrupted_requests_each_consume_an_attempt_on_recovery(
    db: Database,
) -> None:
    class Crash(BaseException):
        pass

    # All three requests are in flight before any of them crashes.
    in_flight = threading.Barrier(3, timeout=10)

    def crash(_request: dict[str, Any]) -> dict[str, Any]:
        in_flight.wait()
        raise Crash()

    scope = "q" * 64
    with pytest.raises(Crash):
        assess_rules(db, scope, FakeLlmClient(fallback=ScriptedResult("ok", build=crash)), 3)
    resumed = FakeLlmClient()
    result = assess_rules(db, scope, resumed, 3)
    statuses: dict[str, list[str]] = {}
    for attempt in result.batch_attempts:
        statuses.setdefault(attempt["batch_id"], []).append(attempt["status"])
    assert list(statuses.values()) == [["interrupted", "succeeded"]] * 3
    assert sorted(c.attempt for c in resumed.calls) == [2, 2, 2]


def test_exhaustion_starts_a_new_explicit_cycle_without_erasing_previous_attempts(
    db: Database,
) -> None:
    client = FakeLlmClient(fallback=ScriptedResult("timeout"))
    for _ in range(2):
        result = assess(db, "c" * 64, client)
        assert {o.state for o in result.outcomes.values()} == {"unassessed"}
    assert len(client.calls) == 6
    rows = db.query(
        "SELECT b.cycle_number,COUNT(*) AS n FROM llm_review_batches b "
        "JOIN llm_review_attempts a ON a.cycle_id=b.cycle_id WHERE b.scope_id=:scope "
        "GROUP BY b.cycle_number ORDER BY b.cycle_number",
        {"scope": "c" * 64},
    )
    assert rows == [{"cycle_number": 1, "n": 3}, {"cycle_number": 2, "n": 3}]


def test_prompt_drift_and_parallel_resume_are_rejected(db: Database) -> None:
    with SqlLlmJournal(db, "d" * 64).locked() as journal:
        journal.configure("original", "audit-drift", "fake", {})
        with (
            connect(CONFIG.sql, CONFIG.sql.test_database) as other,
            pytest.raises(ValueError, match="already running"),
            SqlLlmJournal(other, "d" * 64).locked(),
        ):
            pass
        with pytest.raises(ValueError, match="version bump"):
            journal.configure("changed", "audit-drift", "fake", {})


def test_evaluation_cannot_populate_completed_runs_or_verdicts(db: Database) -> None:
    result = assess_alerts(
        alerts=[v1_row()],
        client=FakeLlmClient(),
        system_prompt="evaluation",
        run_id="e" * 64,
        prompt_version="audit-evaluation",
        model_version="fake",
        now=datetime.now(UTC),
        journal=SqlLlmJournal(db, "e" * 64, kind="evaluation"),
        factored=False,
        reverse_order=True,
    )
    assert result.new_verdicts
    for table in ("runs", "llm_verdicts", "review_publications"):
        row = db.query_one(f"SELECT COUNT(*) AS n FROM {table}")
        assert row and row["n"] == 0
    scope = db.query_one(
        "SELECT scope_kind FROM llm_review_scopes WHERE scope_id=:scope", {"scope": "e" * 64}
    )
    assert scope and scope["scope_kind"] == "evaluation"


def test_published_run_refuses_assessment_before_any_model_call(db: Database) -> None:
    scope = "f" * 64
    persist_run(db, PersistencePayload(run=sample_run(run_id=scope)))
    publish_run(db, scope)
    client = FakeLlmClient()
    with pytest.raises(RunIsPublished):
        assess(db, scope, client)
    assert not client.calls
    assert (
        db.query_one(
            "SELECT 1 AS found FROM llm_review_scopes WHERE scope_id=:scope", {"scope": scope}
        )
        is None
    )


def test_sql_errors_do_not_expose_bound_alert_payloads(db: Database) -> None:
    with pytest.raises(DBAPIError) as caught:
        db.query(
            "SELECT :payload FROM deliberately_missing_audit_table",
            {"payload": "private-alert-document"},
        )
    db.rollback()
    assert "private-alert-document" not in str(caught.value)
