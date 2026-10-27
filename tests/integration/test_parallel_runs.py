"""Parallel runs across teams on real SQL Server (design "Parallel runs across teams")."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier, Event, Lock, Timer
from typing import Any

import pytest
from alerts_bi_shared.db.connection import connect
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from src.app import build_admin
from src.config import AdminConfig, AdminSettings
from src.engine.api import jobs
from src.engine.config import ApiSettings, load_config
from src.engine.db import repositories
from src.engine.db.migrate import reset_test_database
from src.engine.db.repositories import PersistencePayload, persist_run
from src.engine.weekly.runner import WeeklyBusy, schedule_lock

from tests.helpers.sql import sample_run

pytestmark = pytest.mark.integration
CONFIG = load_config()
DB = CONFIG.sql.test_database
CHECKOUT = {"team": "checkout-api", "run_at": "2026-08-24T00:00:00Z", "llm": "fake"}
CHECKOUT_EARLIER = {**CHECKOUT, "run_at": "2026-08-17T00:00:00Z"}
PAYMENTS = {**CHECKOUT, "team": "payments-core"}
FRAUD = {**CHECKOUT, "team": "fraud-detection"}


@pytest.fixture
def settings() -> AdminSettings:
    try:
        with connect(CONFIG.sql, "master") as db:
            db.query("SELECT 1 AS ok")
    except Exception as exc:
        pytest.skip(f"Local SQL Server unavailable: {exc}")
    reset_test_database(CONFIG.sql, DB)
    return AdminSettings(AdminConfig(CONFIG.sql), DB, "p" * 40)


@pytest.fixture
def client(settings: AdminSettings) -> Iterator[TestClient]:
    with TestClient(build_admin(settings), follow_redirects=False) as value:
        yield value


@pytest.fixture
def api(settings: AdminSettings) -> ApiSettings:
    return ApiSettings(CONFIG, database=DB)


def submit(client: TestClient, key: str, body: dict[str, Any]) -> dict[str, Any]:
    token = client.get("/csrf").json()["token"]
    response = client.post(
        "/run-jobs", json=body, headers={"X-CSRF-Token": token, "Idempotency-Key": key}
    )
    assert response.status_code == 202, response.text
    return dict(response.json())


def status(client: TestClient, job: dict[str, Any]) -> dict[str, Any]:
    return dict(client.get(job["status_url"]).json())


def test_two_teams_run_at_once_and_a_team_runs_one_week_at_a_time(
    client: TestClient, api: ApiSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = submit(client, "checkout", CHECKOUT)
    earlier = submit(client, "checkout-earlier", CHECKOUT_EARLIER)
    payments = submit(client, "payments", PAYMENTS)
    assert [first["status"], earlier["status"], payments["status"]] == [
        "pending",
        "queued",
        "queued",
    ]
    other = "d" * 64
    with connect(CONFIG.sql, DB) as db:
        persist_run(db, PersistencePayload(run=sample_run(run_id=other)))

    both = Barrier(3, timeout=30)
    release = Event()
    lock = Lock()
    running: dict[str, str] = {}
    slots: dict[str, int] = {}
    order: list[str] = []

    def fake(_settings: Any, db: Any, body: Any, *, expected_run_id: str) -> None:
        week = f"{body.team} {body.run_at:%m-%d}"
        with lock:
            assert body.team not in running, f"two jobs of {body.team} ran at once"
            running[body.team] = week
            order.append(week)
        row = db.query_one(
            "SELECT active_slot FROM run_jobs WHERE run_id=:id AND status='running'",
            {"id": expected_run_id},
        )
        slots[week] = int(row["active_slot"])
        if week in ("checkout-api 08-24", "payments-core 08-24"):
            both.wait()  # The first two jobs, and the test, meet here while both run.
            assert release.wait(30)
        with lock:
            del running[body.team]

    monkeypatch.setattr(jobs, "execute_request", fake)
    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(jobs.work_parallel, api, 2, poll_seconds=0.1)
        try:
            both.wait()
            assert status(client, first)["status"] == "running"
            assert status(client, payments)["status"] == "running"
            assert status(client, earlier)["status"] == "queued"
            # Every other writer still finds the worker busy while any job runs.
            with pytest.raises(WeeklyBusy), schedule_lock(CONFIG, DB):
                pass
            token = client.get("/csrf").json()["token"]
            assert (
                client.post("/runs", json=FRAUD, headers={"X-CSRF-Token": token}).status_code == 409
            )
            fraud = submit(client, "fraud", FRAUD)
            assert fraud["status"] == "queued"
            # Deletion of another run still goes ahead beside the running jobs.
            deleted = client.post(
                f"/runs/{other}/delete",
                data={"csrf": client.get("/csrf").json()["token"], "confirm_delete": "1"},
            )
            assert deleted.status_code == 303 and "done=" in deleted.headers["location"]
        finally:
            release.set()
        assert worker.result(timeout=120) is True

    assert order[:2] == ["checkout-api 08-24", "payments-core 08-24"]
    assert order.index("checkout-api 08-17") > order.index("checkout-api 08-24")
    assert {slots["checkout-api 08-24"], slots["payments-core 08-24"]} == {1, 2}
    for job in (first, earlier, payments, fraud):
        assert status(client, job)["status"] == "completed"
    with connect(CONFIG.sql, DB) as db:
        assert db.query_one("SELECT job_id FROM run_jobs WHERE active_slot IS NOT NULL") is None


def test_the_database_holds_one_active_job_per_team_in_numbered_slots(
    client: TestClient,
) -> None:
    first = submit(client, "checkout", CHECKOUT)
    earlier = submit(client, "checkout-earlier", CHECKOUT_EARLIER)
    payments = submit(client, "payments", PAYMENTS)
    claim = "UPDATE run_jobs SET status='running',active_slot=:slot,started_at=:at WHERE job_id=:id"
    now = datetime.now(UTC).replace(tzinfo=None)
    with connect(CONFIG.sql, DB) as db:
        assert jobs.get_job(db, first["job_id"])["active_slot"] == 1
        db.execute(claim, {"slot": 2, "at": now, "id": payments["job_id"]})
        db.commit()
        with pytest.raises(IntegrityError):
            # The same team in another slot.
            db.execute(claim, {"slot": 3, "at": now, "id": earlier["job_id"]})
        db.rollback()
        jobs.finish_job(db, payments["job_id"], "completed")
        with pytest.raises(IntegrityError):
            # A slot another job holds.
            db.execute(claim, {"slot": 1, "at": now, "id": payments["job_id"]})
        db.rollback()
        with pytest.raises(IntegrityError):
            db.execute(
                "UPDATE run_jobs SET active_slot=0 WHERE job_id=:id", {"id": first["job_id"]}
            )
        db.rollback()


def test_jobs_left_running_in_several_slots_are_recovered_before_any_claim(
    client: TestClient, api: ApiSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = submit(client, "checkout", CHECKOUT)
    payments = submit(client, "payments", PAYMENTS)
    earlier = submit(client, "checkout-earlier", CHECKOUT_EARLIER)
    now = datetime.now(UTC).replace(tzinfo=None)
    with connect(CONFIG.sql, DB) as db:
        # A worker running two jobs died; its SQL locks are gone with it.
        db.execute(
            "UPDATE run_jobs SET status='running',started_at=:at WHERE job_id=:id",
            {"at": now, "id": first["job_id"]},
        )
        db.execute(
            "UPDATE run_jobs SET status='running',active_slot=2,started_at=:at WHERE job_id=:id",
            {"at": now, "id": payments["job_id"]},
        )
        db.commit()
    ran: list[str] = []
    monkeypatch.setattr(
        jobs,
        "execute_request",
        lambda _settings, _db, body, **_kwargs: ran.append(f"{body.team} {body.run_at:%m-%d}"),
    )

    assert jobs.work_parallel(api, 2, poll_seconds=0.1) is True

    for job in (first, payments):
        recovered = status(client, job)
        assert recovered["status"] == "failed"
        assert "Worker interrupted" in recovered["error_summary"]
    assert ran == ["checkout-api 08-17"]
    assert status(client, earlier)["status"] == "completed"


@pytest.mark.usefixtures("settings")
def test_runs_store_their_results_one_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = "e" * 64
    monkeypatch.setattr(repositories, "PERSIST_LOCK_TIMEOUT_MS", 300)
    with connect(CONFIG.sql, DB) as holder:
        holder.query_one(
            "SET NOCOUNT ON; DECLARE @result INT; EXEC @result = sp_getapplock "
            "@Resource='alerts-bi-persist', @LockMode='Exclusive', @LockOwner='Transaction', "
            "@LockTimeout=0; SELECT @result AS result"
        )
        with connect(CONFIG.sql, DB) as db, pytest.raises(RuntimeError, match="kept storing"):
            persist_run(db, PersistencePayload(run=sample_run(run_id=run_id)))
        holder.rollback()
    with connect(CONFIG.sql, DB) as db:
        assert db.query_one("SELECT run_id FROM runs WHERE run_id=:id", {"id": run_id}) is None
        persist_run(db, PersistencePayload(run=sample_run(run_id=run_id)))
        assert db.query_one("SELECT run_id FROM runs WHERE run_id=:id", {"id": run_id})


@pytest.mark.usefixtures("settings")
def test_waiting_for_the_storage_lock_outlasts_the_sql_request_timeout() -> None:
    """The driver aborts any statement after SQL_REQUEST_TIMEOUT_MS; the lock wait must not."""
    run_id = "f" * 64
    held, release = Event(), Event()

    def hold() -> None:
        with connect(CONFIG.sql, DB) as holder:
            holder.query_one(
                "SET NOCOUNT ON; DECLARE @result INT; EXEC @result = sp_getapplock "
                "@Resource='alerts-bi-persist', @LockMode='Exclusive', "
                "@LockOwner='Transaction', @LockTimeout=0; SELECT @result AS result"
            )
            held.set()
            assert release.wait(30)
            holder.rollback()

    with ThreadPoolExecutor(max_workers=1) as executor:
        holding = executor.submit(hold)
        assert held.wait(10)
        Timer(2.5, release.set).start()
        clock = time.monotonic()
        with connect(replace(CONFIG.sql, request_timeout_ms=1000), DB) as db:
            persist_run(db, PersistencePayload(run=sample_run(run_id=run_id)))
        assert time.monotonic() - clock >= 2
        holding.result(timeout=30)
    with connect(CONFIG.sql, DB) as db:
        assert db.query_one("SELECT run_id FROM runs WHERE run_id=:id", {"id": run_id})
