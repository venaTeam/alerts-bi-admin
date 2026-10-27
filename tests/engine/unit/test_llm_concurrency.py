"""Parallel model requests within a run (design "Parallel model requests within a run")."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from alerts_bi_shared import logging_setup
from alerts_bi_shared.logging_setup import configure_logging, log_context
from src.engine.llm.assess import AssessmentResult, assess_alerts
from src.engine.llm.client import Completion
from src.engine.llm.fake import FakeLlmClient, ScriptedResult
from src.engine.llm.grouping import build_batches

from tests.engine.helpers.rows import v1_row

RUN_ID = "run-1"
PROMPT_VERSION = "1.0.0"
MODEL_VERSION = "fake-model-1"
NOW = datetime(2026, 8, 25, 18, 0, 0, tzinfo=UTC)


def alerts(groups: int) -> list[Any]:
    """Two alerts in each of ``groups`` alert rules, so one batch per rule."""
    return [
        v1_row(key_field=f"k{group}-{n}", alert_rule_url=f"https://g/d/{group}")
        for group in range(groups)
        for n in range(2)
    ]


def batch_ids(rows: list[Any]) -> list[str]:
    return [b.batch_id for b in build_batches(rows, RUN_ID, PROMPT_VERSION, MODEL_VERSION)]


def assess(rows: list[Any], client: Any, **kwargs: Any) -> AssessmentResult:
    return assess_alerts(
        alerts=rows,
        client=client,
        system_prompt="system",
        run_id=RUN_ID,
        prompt_version=PROMPT_VERSION,
        model_version=MODEL_VERSION,
        now=NOW,
        **kwargs,
    )


class Tracking:
    """A fake endpoint that records how many requests are in flight at once."""

    def __init__(
        self, inner: FakeLlmClient | None = None, delay: dict[str, float] | None = None
    ) -> None:
        self.inner = inner or FakeLlmClient(model_version=MODEL_VERSION)
        self.model_version = self.inner.model_version
        self.delay = delay or {}
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.threads: set[str] = set()
        self.answered: list[str] = []

    def complete(
        self, *, system_prompt: str, request_text: str, batch_id: str, attempt: int
    ) -> str | Completion:
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.threads.add(threading.current_thread().name)
        try:
            time.sleep(self.delay.get(batch_id, 0.0))
            return self.inner.complete(
                system_prompt=system_prompt,
                request_text=request_text,
                batch_id=batch_id,
                attempt=attempt,
            )
        finally:
            with self.lock:
                self.in_flight -= 1
                self.answered.append(batch_id)


def comparable(result: AssessmentResult) -> dict[str, Any]:
    """Everything a run stores from the assessment, minus measured durations."""
    return {
        "outcomes": list(result.outcomes.items()),
        "attempts": [
            {key: value for key, value in attempt.items() if key != "duration_ms"}
            for attempt in result.batch_attempts
        ],
        "verdicts": result.new_verdicts,
        "requested": result.requested_batches,
    }


def test_requests_overlap_up_to_the_setting_and_never_beyond() -> None:
    rows = alerts(6)
    client = Tracking(delay=dict.fromkeys(batch_ids(rows), 0.05))

    result = assess(rows, client, concurrency=3)

    assert client.peak == 3
    assert len(client.answered) == 6
    assert all(o.state == "assessed_good" for o in result.outcomes.values())


def test_requests_really_wait_on_the_model_at_the_same_time() -> None:
    rows = alerts(3)
    # Each request waits for the other two; one at a time, the barrier breaks.
    barrier = threading.Barrier(3, timeout=5)

    def together(request: dict[str, Any]) -> dict[str, Any]:
        barrier.wait()
        return FakeLlmClient._build_default(request, ScriptedResult("ok"))

    client = FakeLlmClient(fallback=ScriptedResult("ok", build=together))
    result = assess(rows, client, concurrency=3)
    assert len(result.outcomes) == 6


def test_the_result_matches_one_at_a_time_whatever_order_requests_answer_in() -> None:
    rows = alerts(5)
    ids = batch_ids(rows)
    # The first batch answers last and the last first.
    delay = {batch_id: 0.02 * (len(ids) - index) for index, batch_id in enumerate(ids)}
    failing = FakeLlmClient(model_version=MODEL_VERSION)
    failing.on(ids[1], 1, ScriptedResult("invalid_json"))
    for attempt in (1, 2, 3):
        failing.on(ids[3], attempt, ScriptedResult("transport_error"))

    sequential = assess(rows, Tracking(failing, delay))
    parallel_client = Tracking(failing, delay)
    parallel = assess(rows, parallel_client, concurrency=5)

    assert parallel_client.answered[0] != ids[0], "the requests answered out of batch order"
    assert comparable(parallel) == comparable(sequential)
    assert [attempt["batch_id"] for attempt in parallel.batch_attempts] == [
        ids[0],
        ids[1],
        ids[1],
        ids[2],
        ids[3],
        ids[3],
        ids[3],
        ids[4],
    ]


def test_each_batch_keeps_its_own_retries_and_waits() -> None:
    rows = alerts(4)
    ids = batch_ids(rows)
    inner = FakeLlmClient(model_version=MODEL_VERSION)
    inner.on(ids[2], 1, ScriptedResult("transport_error"))
    inner.on(ids[2], 2, ScriptedResult("timeout"))
    waits: list[float] = []

    result = assess(rows, Tracking(inner), concurrency=4, sleep=waits.append)

    assert len(waits) == 2
    attempts = [a["attempt_number"] for a in result.batch_attempts if a["batch_id"] == ids[2]]
    assert attempts == [1, 2, 3]
    assert len(result.batch_attempts) == 6
    assert all(o.state == "assessed_good" for o in result.outcomes.values())


def test_the_default_sends_one_batch_at_a_time_on_the_callers_thread() -> None:
    rows = alerts(3)
    client = Tracking(delay=dict.fromkeys(batch_ids(rows), 0.01))
    assess(rows, client)
    assert client.peak == 1
    assert client.threads == {threading.current_thread().name}
    assert client.answered == batch_ids(rows)


def test_a_failure_outside_the_retry_policy_stops_the_run() -> None:
    rows = alerts(6)
    ids = batch_ids(rows)

    class Broken(Tracking):
        def complete(self, **kwargs: Any) -> str | Completion:
            if kwargs["batch_id"] == ids[0]:
                raise RuntimeError("endpoint client bug")
            return super().complete(**kwargs)

    client = Broken(delay=dict.fromkeys(ids, 0.05))
    with pytest.raises(RuntimeError, match="endpoint client bug"):
        assess(rows, client, concurrency=2)
    assert client.in_flight == 0, "requests in flight finished before the error propagated"
    assert len(client.answered) < len(ids) - 1, "queued batches were not started"


def test_concurrency_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="concurrency"):
        assess(alerts(1), FakeLlmClient(), concurrency=0)


@pytest.fixture
def json_logs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(logging_setup, "_settings", None)
    configure_logging("worker", level="debug", fmt="json", capture_libraries=False)
    yield


def test_group_records_from_request_threads_carry_the_runs_log_context(
    json_logs: None, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = alerts(4)
    with log_context(team_id="t1", run_id=RUN_ID):
        assess(rows, Tracking(), concurrency=4)

    records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    groups = [record for record in records if record["event"] == "llm.group"]
    assert len(groups) == 4
    assert {(g["team_id"], g["run_id"]) for g in groups} == {("t1", RUN_ID)}
    assert sorted(g["batch_id"] for g in groups) == sorted(batch_ids(rows))
    assert sorted(g["data"]["llm_group"]["group"] for g in groups) == [1, 2, 3, 4]
