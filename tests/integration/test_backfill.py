"""Operator backfill admission, queue order and console flow on real SQL Server (docs/backfill.md)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from alerts_bi_operations.review.publication import publish_run
from alerts_bi_shared.db.connection import Database, connect
from fastapi import HTTPException
from fastapi.testclient import TestClient
from src.app import build_admin
from src.config import AdminConfig, AdminSettings
from src.engine.api import backfill, jobs
from src.engine.api.schemas import RunRequest
from src.engine.config import ApiSettings, load_config
from src.engine.db.migrate import reset_test_database
from src.engine.db.repositories import PersistencePayload, persist_run
from src.engine.weekly import runner
from src.engine.weekly.plan import latest_week_end
from src.runs import delete_run

from tests.helpers.sql import sample_run

pytestmark = pytest.mark.integration
CONFIG = load_config()
DB = CONFIG.sql.test_database
TEAM = "checkout-api"
NOW = datetime(2026, 10, 14, 10, 42, tzinfo=UTC)
WEEK = timedelta(days=7)


def at(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


def naive(value: datetime) -> datetime:
    """Queued jobs carry SQL's naive UTC run_at into the worker's request."""
    return value if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)


@pytest.fixture
def settings(tmp_path_factory: pytest.TempPathFactory) -> AdminSettings:
    try:
        with connect(CONFIG.sql, "master") as db:
            db.query("SELECT 1 AS ok")
    except Exception as exc:
        pytest.skip(f"Local SQL Server unavailable: {exc}")
    reset_test_database(CONFIG.sql, DB)
    return AdminSettings(AdminConfig(CONFIG.sql), DB, "b" * 40)


@pytest.fixture
def api(settings: AdminSettings) -> ApiSettings:
    return ApiSettings(CONFIG, database=DB)


@pytest.fixture
def client(settings: AdminSettings) -> Iterator[TestClient]:
    with TestClient(
        build_admin(settings),
        headers={"X-Forwarded-User": "backfill-tester"},
        follow_redirects=False,
    ) as value:
        yield value


def persist_week(
    calls: list[tuple[str, datetime]], db: Any, body: RunRequest, expected_run_id: str | None
) -> None:
    """Record an executed week and persist a completed run instead of reading alerts."""
    assert body.run_at is not None and expected_run_id is not None
    end = naive(body.run_at)
    calls.append((body.team, end))
    now = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=1)
    persist_run(
        db,
        PersistencePayload(
            run=sample_run(
                run_id=expected_run_id,
                run_at=end,
                window_start=end - WEEK,
                window_end=end,
                started_at=now,
                completed_at=now,
            )
        ),
    )


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, datetime]]:
    calls: list[tuple[str, datetime]] = []

    def fake(
        settings: ApiSettings, db: Any, body: RunRequest, expected_run_id: str | None = None
    ) -> None:
        persist_week(calls, db, body, expected_run_id)

    monkeypatch.setattr(jobs, "execute_request", fake)
    return calls


def queued_weeks(api: ApiSettings, backfill_id: str) -> list[dict[str, Any]]:
    with connect(CONFIG.sql, DB) as db:
        return backfill.backfill_jobs(db, backfill_id)


def test_admission_queues_weeks_oldest_first_without_reserving_the_slot(api: ApiSettings) -> None:
    weeks = [at("2026-08-24"), at("2026-08-10"), at("2026-08-17")]
    row = backfill.submit_backfill(api, TEAM, weeks, "fake", "op", "first", now=NOW)
    assert row["week_count"] == 3 and row["llm_mode"] == "fake"
    stored = queued_weeks(api, row["backfill_id"])
    assert [job["run_at"] for job in stored] == [
        datetime(2026, 8, 10),
        datetime(2026, 8, 17),
        datetime(2026, 8, 24),
    ]
    assert {job["status"] for job in stored} == {"queued"}
    assert {job["active_slot"] for job in stored} == {None}
    assert len({job["run_id"] for job in stored}) == 3
    # Retrying the same form returns the same backfill; other options are refused.
    again = backfill.submit_backfill(api, TEAM, weeks, "fake", "op", "first", now=NOW)
    assert again["backfill_id"] == row["backfill_id"]
    with pytest.raises(HTTPException) as changed:
        backfill.submit_backfill(api, TEAM, weeks[:1], "fake", "op", "first", now=NOW)
    assert changed.value.status_code == 409
    with pytest.raises(HTTPException) as second:
        backfill.submit_backfill(api, TEAM, [at("2026-09-07")], "fake", "op", "second", now=NOW)
    assert second.value.status_code == 409 and "already in progress" in second.value.detail
    # Queued weeks reserve nothing: CLI, report and weekly writers still get the shared lock.
    with runner.schedule_lock(CONFIG, DB):
        pass


def test_admission_refuses_locked_and_unretained_weeks_storing_nothing(
    api: ApiSettings,
) -> None:
    with connect(CONFIG.sql, DB) as db:
        persist_run(
            db,
            PersistencePayload(
                run=sample_run(
                    run_id="c" * 64,
                    run_at=datetime(2026, 8, 17),
                    window_start=datetime(2026, 8, 10),
                    window_end=datetime(2026, 8, 17),
                )
            ),
        )
        publish_run(db, "c" * 64, published_by="op", allow_gap=True)
    manual = jobs.submit_job(
        api, RunRequest(team=TEAM, run_at=at("2026-08-24"), llm="fake"), "op", "manual"
    )
    assert manual["status"] == "pending"
    for weeks, status, message in (
        ([at("2026-08-17")], 409, "Published weeks are locked"),
        ([at("2026-08-24"), at("2026-08-10")], 409, "Already queued or running"),
        ([at("2026-07-27")], 400, "retention"),
        ([at("2026-10-19")], 400, "has not finished"),
        ([], 400, "at least one week"),
    ):
        with pytest.raises(HTTPException) as refused:
            backfill.submit_backfill(
                api, TEAM, weeks, "fake", "op", f"refused-{status}-{len(weeks)}", now=NOW
            )
        assert refused.value.status_code == status and message in refused.value.detail
    with pytest.raises(HTTPException) as unknown:
        backfill.submit_backfill(
            api, "no-such-team", [at("2026-08-10")], "fake", "op", "team", now=NOW
        )
    assert unknown.value.status_code == 400
    with connect(CONFIG.sql, DB) as db:
        assert db.query_one("SELECT COUNT(*) AS n FROM run_backfills") == {"n": 0}


def test_worker_runs_manual_work_before_the_next_week_then_oldest_first(
    api: ApiSettings, executed: list[tuple[str, datetime]], monkeypatch: pytest.MonkeyPatch
) -> None:
    row = backfill.submit_backfill(
        api,
        TEAM,
        [at("2026-08-10"), at("2026-08-17"), at("2026-08-24")],
        "fake",
        "op",
        "order",
        now=NOW,
    )
    manual: dict[str, Any] = {}

    def submit_during_week(
        settings: ApiSettings, db: Any, body: RunRequest, expected_run_id: str | None = None
    ) -> None:
        persist_week(executed, db, body, expected_run_id)
        if body.run_at and naive(body.run_at) == datetime(2026, 8, 17) and not manual:
            # The worker holds the execution lock for this week: a New run queues ahead.
            request = RunRequest(team=TEAM, run_at=at("2026-08-03"), llm="fake")
            manual.update(jobs.submit_job(api, request, "op", "during-week"))

    monkeypatch.setattr(jobs, "execute_request", submit_during_week)
    while jobs.work_once(api):
        pass
    assert manual["status"] == "queued" and manual["active_slot"] is None
    assert [end for _, end in executed] == [
        datetime(2026, 8, 10),
        datetime(2026, 8, 17),
        datetime(2026, 8, 3),
        datetime(2026, 8, 24),
    ]
    with connect(CONFIG.sql, DB) as db:
        assert {job["status"] for job in backfill.backfill_jobs(db, row["backfill_id"])} == {
            "completed"
        }
        assert jobs.get_job(db, manual["job_id"])["status"] == "completed"
        assert db.query_one("SELECT job_id FROM run_jobs WHERE active_slot=1") is None


def test_manual_submission_without_backfill_is_still_refused_while_busy(api: ApiSettings) -> None:
    jobs.submit_job(api, RunRequest(team=TEAM, run_at=at("2026-08-24"), llm="fake"), "op", "one")
    with pytest.raises(HTTPException) as busy:
        jobs.submit_job(
            api, RunRequest(team=TEAM, run_at=at("2026-08-17"), llm="fake"), "op", "two"
        )
    assert busy.value.status_code == 409


def hold_applock(resource: str) -> ExitStack:
    stack = ExitStack()
    db = stack.enter_context(connect(CONFIG.sql, DB, autocommit=True))
    result = db.query_one(
        "SET NOCOUNT ON; DECLARE @result INT; EXEC @result = sp_getapplock @Resource=:resource, "
        "@LockMode='Exclusive', @LockOwner='Session', @LockTimeout=0; SELECT @result AS result",
        {"resource": resource},
    )
    assert result is not None and int(result["result"]) >= 0
    return stack


def applock_free(resource: str) -> bool:
    with connect(CONFIG.sql, DB, autocommit=True) as db:
        row = db.query_one(
            "SELECT APPLOCK_TEST('public', :resource, 'Exclusive', 'Session') AS free",
            {"resource": resource},
        )
    return row is not None and int(row["free"]) == 1


def test_waiting_weekly_invocation_keeps_the_worker_off_backfill_weeks(
    api: ApiSettings, executed: list[tuple[str, datetime]], monkeypatch: pytest.MonkeyPatch
) -> None:
    backfill.submit_backfill(api, TEAM, [at("2026-08-10")], "fake", "op", "yield", now=NOW)
    held = hold_applock(runner.BACKFILL_YIELD_RESOURCE)
    with monkeypatch.context() as patch:

        def unexpected(*args: Any, **kwargs: Any) -> Any:
            pytest.fail("A yielded backfill must not contend for the execution lock")

        patch.setattr(jobs, "execution_lock", unexpected)
        assert jobs.work_once(api) is False
    held.close()
    assert executed == []
    assert jobs.work_once(api) is True
    assert executed == [(TEAM, datetime(2026, 8, 10))]


def test_weekly_lock_waits_for_the_running_week_only_during_a_backfill(api: ApiSettings) -> None:
    row = backfill.submit_backfill(api, TEAM, [at("2026-08-10")], "fake", "op", "wait", now=NOW)
    busy = ExitStack()
    busy.enter_context(runner.schedule_lock(CONFIG, DB))
    sleeps: list[float] = []

    def finish_week(seconds: float) -> None:
        sleeps.append(seconds)
        assert not applock_free(runner.BACKFILL_YIELD_RESOURCE)
        busy.close()

    with runner.weekly_lock(CONFIG, DB, poll_seconds=0.5, sleep=finish_week):
        assert sleeps == [0.5]
    assert applock_free(runner.BACKFILL_YIELD_RESOURCE)
    # Without a backfill in progress a busy lock still refuses at once.
    with connect(CONFIG.sql, DB) as db:
        backfill.cancel_backfill(db, row["backfill_id"], "op")
    with runner.schedule_lock(CONFIG, DB):

        def never(seconds: float) -> None:
            pytest.fail("No backfill is in progress, so the weekly run must not wait")

        with pytest.raises(runner.WeeklyBusy), runner.weekly_lock(CONFIG, DB, sleep=never):
            pass


def test_weekly_lock_gives_up_after_the_wait(api: ApiSettings) -> None:
    backfill.submit_backfill(api, TEAM, [at("2026-08-10")], "fake", "op", "give-up", now=NOW)
    clock = iter([0.0, 10.0, 7300.0, 7300.0])
    with (
        runner.schedule_lock(CONFIG, DB),
        pytest.raises(runner.WeeklyBusy),
        runner.weekly_lock(
            CONFIG,
            DB,
            wait=timedelta(hours=2),
            sleep=lambda seconds: None,
            clock=lambda: next(clock),
        ),
    ):
        pass
    assert applock_free(runner.BACKFILL_YIELD_RESOURCE)


def post(client: TestClient, path: str, data: dict[str, Any] | None = None) -> Any:
    token = client.get("/csrf").json()["token"]
    return client.post(path, data={"csrf": token, **(data or {})})


def test_console_backfill_flow_from_dialog_to_dismissal(
    client: TestClient,
    api: ApiSettings,
    executed: list[tuple[str, datetime]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latest = latest_week_end(datetime.now(UTC))
    first, second, third = latest - 3 * WEEK, latest - 2 * WEEK, latest - WEEK
    page = client.get(f"/teams/{TEAM}")
    assert page.status_code == 200
    assert 'id="backfill-weeks"' in page.text and f'value="{third:%Y-%m-%d}"' in page.text
    assert "inside Elasticsearch retention have no run" in page.text
    weeks = [f"{day:%Y-%m-%d}" for day in (third, first, second)]
    accepted = post(
        client,
        f"/teams/{TEAM}/backfill",
        {"week": weeks, "llm": "fake", "submission_key": "form-1"},
    )
    assert accepted.status_code == 303, accepted.text
    assert "Backfill+accepted.+3+runs+queued" in accepted.headers["location"]
    again = post(
        client,
        f"/teams/{TEAM}/backfill",
        {"week": weeks, "llm": "fake", "submission_key": "form-1"},
    )
    assert "done=" in again.headers["location"]
    page = client.get(f"/teams/{TEAM}").text
    assert "Backfill · 3 weeks" in page and "data-backfill-url" in page
    assert "A backfill is already in progress for this team" in page
    with connect(CONFIG.sql, DB) as db:
        stored = db.query_one("SELECT backfill_id FROM run_backfills")
    assert stored is not None
    backfill_id = str(stored["backfill_id"])
    status = client.get(f"/backfills/{backfill_id}").json()
    assert status["active"] is True and status["llm_mode"] == "fake"
    assert [week["status"] for week in status["weeks"]] == ["queued"] * 3
    assert status["weeks"][0]["week_end"] == f"{first:%Y-%m-%d}T00:00:00.000Z"
    assert "requested_by" not in status
    oldest_job = status["weeks"][0]["job_id"]
    removed = post(client, f"/run-jobs/{oldest_job}/cancel")
    assert "Week+removed" in removed.headers["location"]

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Test failure")

    with monkeypatch.context() as patch:
        patch.setattr(jobs, "execute_request", fail)
        assert jobs.work_once(api) is True
    assert jobs.work_once(api) is True
    assert executed == [(TEAM, third.replace(tzinfo=None))]
    weeks_now = client.get(f"/backfills/{backfill_id}").json()["weeks"]
    assert [week["status"] for week in weeks_now] == ["cancelled", "failed", "completed"]
    assert "retry this week" in weeks_now[1]["error_summary"]
    assert weeks_now[2]["result_url"].endswith("#selected-run")
    page = client.get(f"/teams/{TEAM}").text
    assert "Finished with 1 failed" in page and "Retry failed week" in page
    assert "Backfill</small>" in page or "· Backfill" in page
    retried = post(client, f"/backfills/{backfill_id}/retry")
    assert "Retry+queued+for+1+failed+week" in retried.headers["location"]
    weeks_now = client.get(f"/backfills/{backfill_id}").json()["weeks"]
    assert weeks_now[1]["status"] == "queued" and weeks_now[1]["attempts"] == 2
    refused = post(client, f"/backfills/{backfill_id}/dismiss")
    assert "error=" in refused.headers["location"]
    cancelled = post(client, f"/backfills/{backfill_id}/cancel")
    assert "1+queued+week+cancelled" in cancelled.headers["location"]
    dismissed = post(client, f"/backfills/{backfill_id}/dismiss")
    assert "Backfill+dismissed" in dismissed.headers["location"]
    page = client.get(f"/teams/{TEAM}").text
    assert "data-backfill-url" not in page and "Backfill · 3 weeks" not in page
    assert client.post(f"/backfills/{backfill_id}/cancel", data={}).status_code == 403
    assert client.get("/backfills/" + "0" * 32).status_code == 404


def test_queued_manual_runs_keep_submission_order(
    api: ApiSettings, executed: list[tuple[str, datetime]]
) -> None:
    backfill.submit_backfill(api, TEAM, [at("2026-08-10")], "fake", "op", "manual-order", now=NOW)
    with connect(CONFIG.sql, DB) as db:
        week = jobs.claim_queued(db, datetime.now(UTC).replace(tzinfo=None))
    assert week is not None
    # Submitted first for the newer week, then for an older one: submission order wins.
    first = jobs.submit_job(
        api, RunRequest(team=TEAM, run_at=at("2026-10-12"), llm="fake"), "op", "first-manual"
    )
    second = jobs.submit_job(
        api, RunRequest(team=TEAM, run_at=at("2026-08-03"), llm="fake"), "op", "second-manual"
    )
    assert first["status"] == second["status"] == "queued"
    with connect(CONFIG.sql, DB) as db:
        jobs.finish_job(db, str(week["job_id"]), "completed")
    while jobs.work_once(api):
        pass
    assert [end for _, end in executed] == [datetime(2026, 10, 12), datetime(2026, 8, 3)]


class PausedDeletion(Database):
    """Stops a deletion after its checks, before it removes any row."""

    def __init__(self, db: Database, paused: Event, release: Event) -> None:
        super().__init__(db.connection)
        self.paused, self.release = paused, release

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        if sql.startswith("DELETE a FROM llm_review_attempts") and not self.paused.is_set():
            self.paused.set()
            assert self.release.wait(30), "The test must release the paused deletion"
        super().execute(sql, params)


@pytest.mark.parametrize("admission", ["backfill", "manual"])
def test_deletion_cannot_remove_a_week_queued_while_it_runs(
    api: ApiSettings, admission: str
) -> None:
    # Seed a run for the week ending 10 August with the identity a new admission computes.
    seed = backfill.submit_backfill(api, TEAM, [at("2026-08-10")], "fake", "op", "seed", now=NOW)
    target = queued_weeks(api, seed["backfill_id"])[0]
    with connect(CONFIG.sql, DB) as db:
        backfill.cancel_backfill(db, seed["backfill_id"], "op")
        persist_run(
            db,
            PersistencePayload(
                run=sample_run(
                    run_id=target["run_id"],
                    run_at=datetime(2026, 8, 10),
                    window_start=datetime(2026, 8, 3),
                    window_end=datetime(2026, 8, 10),
                )
            ),
        )
    if admission == "manual":
        # A queued backfill week keeps a backfill in progress, so a busy manual run queues.
        backfill.submit_backfill(api, TEAM, [at("2026-08-24")], "fake", "op", "other", now=NOW)
    paused, release = Event(), Event()

    def delete() -> str:
        # The console holds the shared lock around deletion, which makes manual admission
        # fall back to queueing without the execution lock.
        with (
            runner.schedule_lock(CONFIG, DB, allow_manual_job=True),
            connect(CONFIG.sql, DB) as db,
        ):
            return delete_run(PausedDeletion(db, paused, release), str(target["run_id"]))

    def admit() -> dict[str, Any]:
        if admission == "backfill":
            row = backfill.submit_backfill(
                api, TEAM, [at("2026-08-10")], "fake", "op", "during-delete", now=NOW
            )
            return queued_weeks(api, row["backfill_id"])[0]
        request = RunRequest(team=TEAM, run_at=at("2026-08-10"), llm="fake")
        return jobs.submit_job(api, request, "op", "during-delete")

    with ThreadPoolExecutor(max_workers=2) as pool:
        deletion = pool.submit(delete)
        try:
            assert paused.wait(30)
            accepted = pool.submit(admit)
            time.sleep(1.5)
            assert not accepted.done(), "Admission must wait for the deletion to finish"
        finally:
            release.set()
        assert deletion.result(timeout=60) == TEAM
        job = accepted.result(timeout=60)
    assert job["status"] == "queued" and job["run_id"] == target["run_id"]
    with connect(CONFIG.sql, DB) as db:
        assert jobs.get_job(db, str(job["job_id"]))["status"] == "queued"
        assert (
            db.query_one("SELECT run_id FROM runs WHERE run_id=:id", {"id": target["run_id"]})
            is None
        )


def test_retried_backfill_stays_visible_after_dismissal_or_a_newer_backfill(
    client: TestClient,
    api: ApiSettings,
    executed: list[tuple[str, datetime]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latest = latest_week_end(datetime.now(UTC))
    older = backfill.submit_backfill(api, TEAM, [latest - 3 * WEEK], "fake", "op", "older")
    panel = f'data-backfill-url="/backfills/{older["backfill_id"]}"'

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Test failure")

    def run_failing_week() -> None:
        with monkeypatch.context() as patch:
            patch.setattr(jobs, "execute_request", fail)
            assert jobs.work_once(api)

    run_failing_week()
    # One tab dismisses the failed backfill; another still holds its Retry form.
    dismissed = post(client, f"/backfills/{older['backfill_id']}/dismiss")
    assert "Backfill+dismissed" in dismissed.headers["location"]
    assert "Backfill · 1 week" not in client.get(f"/teams/{TEAM}").text
    retried = post(client, f"/backfills/{older['backfill_id']}/retry")
    assert "Retry+queued" in retried.headers["location"]
    page = client.get(f"/teams/{TEAM}").text
    assert panel in page and "A backfill is already in progress for this team" in page
    with connect(CONFIG.sql, DB) as db:
        assert backfill.get_backfill(db, older["backfill_id"])["dismissed_at"] is None
    # A newer backfill finishes after the older one fails again; retrying the older one
    # must still show its progress ahead of the newer finished panel.
    run_failing_week()
    backfill.submit_backfill(api, TEAM, [latest - 2 * WEEK], "fake", "op", "newer")
    assert jobs.work_once(api)
    assert executed == [(TEAM, (latest - 2 * WEEK).replace(tzinfo=None))]
    assert panel not in client.get(f"/teams/{TEAM}").text
    post(client, f"/backfills/{older['backfill_id']}/retry")
    assert panel in client.get(f"/teams/{TEAM}").text


def test_retry_still_queues_after_an_earlier_attempts_run_is_deleted(
    settings: AdminSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "teams.json"
    registry.write_bytes(Path("config/teams.json").read_bytes())
    api = ApiSettings(CONFIG, database=DB, registry_path=str(registry))

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Test failure")

    monkeypatch.setattr(jobs, "execute_request", fail)
    row = backfill.submit_backfill(api, TEAM, [at("2026-08-10")], "fake", "op", "retry", now=NOW)
    first = queued_weeks(api, row["backfill_id"])[0]
    with connect(CONFIG.sql, DB) as db:
        # The week already had an unpublished run with the first attempt's identity.
        persist_run(
            db,
            PersistencePayload(
                run=sample_run(
                    run_id=first["run_id"],
                    run_at=datetime(2026, 8, 10),
                    window_start=datetime(2026, 8, 3),
                    window_end=datetime(2026, 8, 10),
                )
            ),
        )
    assert jobs.work_once(api)
    # A registry change gives the second attempt a different run identity.
    registry.write_bytes(registry.read_bytes() + b"\n")
    backfill.retry_backfill(api, row["backfill_id"], "op", now=NOW)
    assert jobs.work_once(api)
    attempts = queued_weeks(api, row["backfill_id"])
    assert [job["status"] for job in attempts] == ["failed", "failed"]
    assert attempts[0]["run_id"] != attempts[1]["run_id"]
    with connect(CONFIG.sql, DB) as db:
        # Deleting the first run also removes the first attempt's job record.
        assert delete_run(db, str(first["run_id"])) == TEAM
    backfill.retry_backfill(api, row["backfill_id"], "op", now=NOW)
    remaining = queued_weeks(api, row["backfill_id"])
    assert [job["status"] for job in remaining] == ["failed", "queued"]
    assert len({job["idempotency_key"] for job in remaining}) == 2


def test_queued_week_blocks_deleting_the_run_it_would_recreate(
    client: TestClient, api: ApiSettings
) -> None:
    row = backfill.submit_backfill(api, TEAM, [at("2026-08-10")], "fake", "op", "delete", now=NOW)
    job = queued_weeks(api, row["backfill_id"])[0]
    with connect(CONFIG.sql, DB) as db:
        persist_run(
            db,
            PersistencePayload(
                run=sample_run(
                    run_id=job["run_id"],
                    run_at=datetime(2026, 8, 10),
                    window_start=datetime(2026, 8, 3),
                    window_end=datetime(2026, 8, 10),
                )
            ),
        )
    refused = post(client, f"/runs/{job['run_id']}/delete", {"confirm_delete": "1"})
    assert "queued+run+would+recreate" in refused.headers["location"]
    with connect(CONFIG.sql, DB) as db:
        assert db.query_one("SELECT run_id FROM runs") == {"run_id": job["run_id"]}


def test_queued_manual_status_is_public_and_shown_in_the_workspace(
    client: TestClient, api: ApiSettings
) -> None:
    backfill.submit_backfill(api, TEAM, [at("2026-08-10")], "fake", "op", "status", now=NOW)
    with connect(CONFIG.sql, DB) as db:
        claimed = jobs.claim_queued(db, datetime.now(UTC).replace(tzinfo=None))
    assert claimed is not None and claimed["status"] == "running"
    token = client.get("/csrf").json()["token"]
    response = client.post(
        "/run-jobs",
        json={"team": TEAM, "run_at": "2026-08-24T00:00:00Z", "llm": "fake"},
        headers={"X-CSRF-Token": token, "Idempotency-Key": "queued-json"},
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued"
    page = client.get(f"/teams/{TEAM}").text
    assert "Queued. It starts before the next backfill week." in page


def test_interrupted_week_fails_and_asks_to_retry_the_week(api: ApiSettings) -> None:
    row = backfill.submit_backfill(api, TEAM, [at("2026-08-17")], "fake", "op", "lost", now=NOW)
    with connect(CONFIG.sql, DB) as db:
        # A worker claimed the week, then died before committing any analysis.
        claimed = jobs.claim_queued(db, datetime.now(UTC).replace(tzinfo=None))
    assert claimed is not None and claimed["backfill_id"] == row["backfill_id"]
    assert jobs.work_once(api) is False
    (week,) = queued_weeks(api, row["backfill_id"])
    assert week["status"] == "failed" and week["active_slot"] is None
    assert week["error_summary"] == "Worker interrupted. Retry this week."
