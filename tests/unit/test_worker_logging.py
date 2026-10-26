"""What the run worker records when it starts, fails and waits (design "Console log format")."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from alerts_bi_shared import logging_setup
from alerts_bi_shared.logging_setup import configure_logging
from src.engine.api import jobs
from src.engine.db.llm_audit import ReviewSettingsChanged


@pytest.fixture(autouse=True)
def console_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(logging_setup, "_settings", None)
    configure_logging("worker", level="info", fmt="console", color=False, capture_libraries=False)
    yield


def test_a_malformed_es_url_does_not_stop_the_worker_starting() -> None:
    assert jobs._host_and_port("https://user:pw@es.internal:9200/x") == "es.internal:9200"
    assert jobs._host_and_port("https://es.internal:92OO") == "(invalid ES_URL)"


class _Db:
    def query_one(self, *_args: Any, **_kwargs: Any) -> None:
        return None  # The run is not published.

    def rollback(self) -> None:
        pass


def test_the_failure_is_recorded_even_when_the_job_update_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_run(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("Elasticsearch is unreachable")

    def fail_update(*_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError("the SQL connection dropped")

    monkeypatch.setattr(jobs, "load_registry", lambda *_: SimpleNamespace(file_sha256="r"))
    monkeypatch.setattr(jobs, "execute_request", fail_run)
    monkeypatch.setattr(jobs, "committed_result", lambda *_: False)
    monkeypatch.setattr(jobs, "finish_job", fail_update)
    job = {
        "job_id": "7c1e9a2f" + "0" * 24,
        "team_id": "t1",
        "run_id": "a" * 64,
        "run_at": datetime(2026, 8, 24),
        "llm_mode": "fake",
        "registry_sha256": "r",
        "submitted_at": datetime(2026, 8, 24, 0, 0, 0),
        "started_at": datetime(2026, 8, 24, 0, 0, 4),
    }

    with pytest.raises(ConnectionError):
        jobs._run_job(SimpleNamespace(registry_path="x"), _Db(), job)  # type: ignore[arg-type]

    err = capsys.readouterr().err
    assert "run.job_failed     The manual run failed after" in err
    assert "    RuntimeError: Elasticsearch is unreachable" in err


def test_changed_model_settings_tell_the_operator_to_delete_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def changed_settings(*_args: Any, **_kwargs: Any) -> None:
        raise ReviewSettingsChanged("settings changed")

    finished: list[tuple[str, str | None]] = []
    monkeypatch.setattr(jobs, "load_registry", lambda *_: SimpleNamespace(file_sha256="r"))
    monkeypatch.setattr(jobs, "execute_request", changed_settings)
    monkeypatch.setattr(jobs, "committed_result", lambda *_: False)
    monkeypatch.setattr(
        jobs, "finish_job", lambda _db, _id, status, advice=None: finished.append((status, advice))
    )
    job = {
        "job_id": "7c1e9a2f" + "0" * 24,
        "team_id": "t1",
        "run_id": "a" * 64,
        "run_at": datetime(2026, 8, 24),
        "llm_mode": "live",
        "registry_sha256": "r",
        "submitted_at": datetime(2026, 8, 24, 0, 0, 0),
        "started_at": datetime(2026, 8, 24, 0, 0, 4),
    }

    for backfill in (None, "b" * 32):
        jobs._run_job(SimpleNamespace(registry_path="x"), _Db(), {**job, "backfill_id": backfill})  # type: ignore[arg-type]

    advice = (
        "The model settings changed since this week was first sent to the model. "
        "Delete this run, then run the week again."
    )
    assert finished == [("failed", advice), ("failed", advice)]


def test_an_idle_worker_says_so_every_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([0.0, 5.0, 10.0, 16.0, 20.0, 33.0])
    polls = iter([False, False, True, False, False])
    idle: list[float] = []

    def work_once(_settings: Any) -> bool:
        try:
            return next(polls)
        except StopIteration:
            raise SystemExit from None

    monkeypatch.setattr(jobs, "work_once", work_once)
    monkeypatch.setattr(jobs, "_log_worker_started", lambda *_: None)
    monkeypatch.setattr(jobs, "_log_idle", lambda _settings, seconds: idle.append(seconds))
    monkeypatch.setattr(
        jobs, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _s: None)
    )

    with pytest.raises(SystemExit):
        jobs.run_worker(SimpleNamespace(), heartbeat_seconds=10)  # type: ignore[arg-type]

    # Idle 0-10s says so once; work at 16s restarts the count; idle again from 16 to 33s.
    assert idle == [10.0, 17.0]
