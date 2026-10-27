"""How the run worker fills its slots (design "Parallel runs across teams")."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from typing import Any

import pytest
from alerts_bi_shared import logging_setup
from alerts_bi_shared.logging_setup import configure_logging
from sqlalchemy.exc import DBAPIError
from src.engine.api import jobs


class _Db:
    """The coordinator's connection: it only sets its deadlock priority and rolls back."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.rollbacks = 0

    def execute(self, sql: str, *_args: Any) -> None:
        self.statements.append(sql)

    def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture
def console_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logging_setup, "_settings", None)
    configure_logging("worker", level="info", fmt="console", color=False, capture_libraries=False)


def _events(err: str, event: str) -> list[str]:
    return [line for line in err.splitlines() if f" {event} " in line]


def _deadlock() -> DBAPIError:
    return DBAPIError("UPDATE run_jobs", {}, Exception("chosen as deadlock victim"))


class _Claims:
    """Hands out jobs the way ``_claim_next`` does: each in the slot it is asked for."""

    def __init__(self, *teams: str) -> None:
        self.waiting = list(teams)
        self.claimed: list[tuple[str, int]] = []

    def __call__(self, _db: Any, slot: int) -> dict[str, Any] | None:
        if not self.waiting:
            return None
        team = self.waiting.pop(0)
        self.claimed.append((team, slot))
        return {"team_id": team, "active_slot": slot, "run_id": team[0] * 64, "job_id": team}


def test_jobs_run_side_by_side_and_a_freed_slot_takes_the_next_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = _Claims("checkout-api", "payments-core", "fraud-detection")
    together = Barrier(2, timeout=10)
    lock = Lock()
    active: set[str] = set()
    most = 0

    def run(_settings: Any, job: dict[str, Any]) -> None:
        nonlocal most
        with lock:
            active.add(job["team_id"])
            most = max(most, len(active))
        if job["team_id"] != "fraud-detection":
            together.wait()  # Both first jobs must be running at once to get past this.
        with lock:
            active.discard(job["team_id"])

    monkeypatch.setattr(jobs, "_claim_next", claims)
    monkeypatch.setattr(jobs, "_run_on_own_connection", run)

    assert jobs._run_slots(SimpleNamespace(), _Db(), 2, 0.05) is True  # type: ignore[arg-type]

    assert most == 2
    assert claims.claimed[:2] == [("checkout-api", 1), ("payments-core", 2)]
    assert claims.claimed[2][0] == "fraud-detection" and claims.claimed[2][1] in (1, 2)


def test_a_poll_starts_work_submitted_while_a_job_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    claims = _Claims("checkout-api")
    started_second = Event()

    def run(_settings: Any, job: dict[str, Any]) -> None:
        if job["team_id"] == "checkout-api":
            claims.waiting.append("payments-core")  # Submitted after the first claim.
            assert started_second.wait(10), "the second job must start while the first runs"
        else:
            started_second.set()

    monkeypatch.setattr(jobs, "_claim_next", claims)
    monkeypatch.setattr(jobs, "_run_on_own_connection", run)

    assert jobs._run_slots(SimpleNamespace(), _Db(), 2, 0.05) is True  # type: ignore[arg-type]
    assert [team for team, _slot in claims.claimed] == ["checkout-api", "payments-core"]


@pytest.mark.usefixtures("console_log")
def test_an_infrastructure_error_stops_claims_and_exits_after_the_others_finish(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    claims = _Claims("checkout-api", "payments-core", "fraud-detection")
    failed = Event()
    finished: list[str] = []

    def run(_settings: Any, job: dict[str, Any]) -> None:
        if job["team_id"] == "checkout-api":
            failed.set()
            raise ConnectionError("the SQL connection dropped")
        assert failed.wait(10)
        finished.append(job["team_id"])

    monkeypatch.setattr(jobs, "_claim_next", claims)
    monkeypatch.setattr(jobs, "_run_on_own_connection", run)

    with pytest.raises(ConnectionError):
        jobs._run_slots(SimpleNamespace(), _Db(), 2, 0.05)  # type: ignore[arg-type]

    assert finished == ["payments-core"]
    assert claims.waiting == ["fraud-detection"]
    stopping = _events(capsys.readouterr().err, "worker.stopping")
    assert len(stopping) == 1
    assert "The job in slot 1 stopped" in stopping[0] and "team=checkout-api" in stopping[0]


@pytest.mark.usefixtures("console_log")
def test_every_job_lost_while_stopping_is_named(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    claims = _Claims("checkout-api", "payments-core")
    first = Event()

    def run(_settings: Any, job: dict[str, Any]) -> None:
        if job["team_id"] == "checkout-api":
            first.set()
            raise ConnectionError("the SQL server failed over")
        assert first.wait(10)
        raise ConnectionError("the SQL server failed over")

    monkeypatch.setattr(jobs, "_claim_next", claims)
    monkeypatch.setattr(jobs, "_run_on_own_connection", run)

    with pytest.raises(ConnectionError):
        jobs._run_slots(SimpleNamespace(), _Db(), 2, 0.05)  # type: ignore[arg-type]

    err = capsys.readouterr().err
    named = _events(err, "worker.stopping") + _events(err, "worker.job_lost")
    assert len(named) == 2
    assert {"team=checkout-api" in line for line in named} == {True, False}
    assert any("team=payments-core" in line for line in named)


def test_the_coordinator_loses_deadlocks_to_deletions(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _Db()
    monkeypatch.setattr(jobs, "_claim_next", _Claims())
    jobs._run_slots(SimpleNamespace(), db, 2, 0.05)  # type: ignore[arg-type]
    assert db.statements == ["SET DEADLOCK_PRIORITY LOW"]


@pytest.mark.usefixtures("console_log")
def test_a_failed_claim_is_retried_at_the_next_poll(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    claims = _Claims("checkout-api", "payments-core")
    calls = 0
    second_started = Event()

    def claim(db: Any, slot: int) -> dict[str, Any] | None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise _deadlock()  # Claiming payments-core collides with a deletion once.
        return claims(db, slot)

    def run(_settings: Any, job: dict[str, Any]) -> None:
        if job["team_id"] == "checkout-api":
            assert second_started.wait(10), "the retried claim must start beside the first job"
        else:
            second_started.set()

    db = _Db()
    monkeypatch.setattr(jobs, "_claim_next", claim)
    monkeypatch.setattr(jobs, "_run_on_own_connection", run)

    assert jobs._run_slots(SimpleNamespace(), db, 2, 0.05) is True  # type: ignore[arg-type]

    assert [team for team, _slot in claims.claimed] == ["checkout-api", "payments-core"]
    assert db.rollbacks == 1
    assert len(_events(capsys.readouterr().err, "worker.claim_failed")) == 1


@pytest.mark.usefixtures("console_log")
def test_claims_failing_three_times_stop_the_worker_after_the_running_jobs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    claims = _Claims("checkout-api")
    calls = 0
    gave_up = Event()
    finished: list[str] = []

    def claim(db: Any, slot: int) -> dict[str, Any] | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return claims(db, slot)
        if calls == 1 + jobs.CLAIM_ATTEMPTS:
            gave_up.set()
        raise _deadlock()

    def run(_settings: Any, job: dict[str, Any]) -> None:
        assert gave_up.wait(10)
        finished.append(job["team_id"])

    monkeypatch.setattr(jobs, "_claim_next", claim)
    monkeypatch.setattr(jobs, "_run_on_own_connection", run)

    with pytest.raises(DBAPIError):
        jobs._run_slots(SimpleNamespace(), _Db(), 2, 0.05)  # type: ignore[arg-type]

    assert finished == ["checkout-api"]
    assert calls == 1 + jobs.CLAIM_ATTEMPTS
    err = capsys.readouterr().err
    assert len(_events(err, "worker.claim_failed")) == jobs.CLAIM_ATTEMPTS - 1
    assert "failed 3 times in a row" in _events(err, "worker.stopping")[0]


def test_once_runs_only_the_first_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    claims = _Claims("checkout-api", "payments-core", "fraud-detection")
    monkeypatch.setattr(jobs, "_claim_next", claims)
    monkeypatch.setattr(jobs, "_run_on_own_connection", lambda _settings, _job: None)

    assert jobs._run_slots(SimpleNamespace(), _Db(), 2, 0.05, refill=False) is True  # type: ignore[arg-type]

    assert claims.claimed == [("checkout-api", 1), ("payments-core", 2)]
    assert claims.waiting == ["fraud-detection"]


def test_no_claimable_job_means_no_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "_claim_next", _Claims())
    assert jobs._run_slots(SimpleNamespace(), _Db(), 3, 0.05) is False  # type: ignore[arg-type]


@pytest.mark.parametrize("concurrency", [0, -1, 9])
def test_concurrency_outside_one_to_eight_is_refused(concurrency: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 8"):
        jobs.check_concurrency(SimpleNamespace(), concurrency)  # type: ignore[arg-type]


def test_concurrency_one_needs_no_database() -> None:
    jobs.check_concurrency(SimpleNamespace(), 1)  # type: ignore[arg-type]


@pytest.mark.parametrize(("applied", "refused"), [(None, True), ({"ready": 1}, False)])
def test_parallel_slots_need_migration_021(
    monkeypatch: pytest.MonkeyPatch, applied: dict[str, int] | None, refused: bool
) -> None:
    asked: list[dict[str, Any]] = []

    class _Db:
        def query_one(self, _sql: str, params: dict[str, Any]) -> dict[str, int] | None:
            asked.append(params)
            return applied

    @contextmanager
    def connect(*_args: Any) -> Iterator[_Db]:
        yield _Db()

    monkeypatch.setattr(jobs, "connect", connect)
    settings = SimpleNamespace(config=SimpleNamespace(sql=None), target_database="alerts_bi")
    if refused:
        with pytest.raises(ValueError, match="needs database migration 021_parallel_run_slots"):
            jobs.check_concurrency(settings, 3)  # type: ignore[arg-type]
    else:
        jobs.check_concurrency(settings, 3)  # type: ignore[arg-type]
    assert asked == [{"version": "021_parallel_run_slots"}]
