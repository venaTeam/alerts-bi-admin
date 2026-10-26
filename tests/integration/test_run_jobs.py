"""Durable submission/worker/recovery behavior on real SQL Server and Elasticsearch."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import pytest
from alerts_bi_operations.report.render import build_run_outputs
from alerts_bi_shared.db.connection import connect
from fastapi.testclient import TestClient
from src.app import build_admin
from src.config import AdminConfig, AdminSettings
from src.engine.api import jobs, service
from src.engine.api.schemas import RunRequest
from src.engine.config import ApiSettings, load_config
from src.engine.db.migrate import reset_test_database
from src.engine.db.repositories import PersistencePayload, persist_run

from tests.helpers.sql import sample_daily, sample_run

pytestmark = pytest.mark.integration
CONFIG = load_config()
DB = CONFIG.sql.test_database
BODY = {"team": "checkout-api", "run_at": "2026-08-24T00:00:00Z", "llm": "fake"}


@pytest.fixture
def settings(tmp_path_factory: pytest.TempPathFactory) -> AdminSettings:
    # Fail migration regressions when SQL is reachable instead of hiding them as skips.
    try:
        with connect(CONFIG.sql, "master") as db:
            db.query("SELECT 1 AS ok")
    except Exception as exc:
        pytest.skip(f"Local SQL Server unavailable: {exc}")
    reset_test_database(CONFIG.sql, DB)
    return AdminSettings(AdminConfig(CONFIG.sql), DB, "j" * 40)


@pytest.fixture
def client(settings: AdminSettings) -> Iterator[TestClient]:
    with TestClient(build_admin(settings), follow_redirects=False) as value:
        yield value


def submit(client: TestClient, key: str, body: dict[str, Any] | None = None) -> Any:
    token = client.get("/csrf").json()["token"]
    return client.post(
        "/run-jobs", json=body or BODY, headers={"X-CSRF-Token": token, "Idempotency-Key": key}
    )


def test_acceptance_is_durable_idempotent_and_does_not_execute(client: TestClient) -> None:
    first = submit(client, "accepted")
    assert first.status_code == 202, first.text
    row = first.json()
    assert row["status"] == "pending" and row["result_url"] is None
    assert first.headers["location"] == row["status_url"]
    assert submit(client, "accepted").json()["job_id"] == row["job_id"]
    assert submit(client, "accepted", {**BODY, "llm": "off"}).status_code == 409
    duplicate = submit(client, "another")
    assert duplicate.status_code == 409
    assert "Already queued or running for this team" in duplicate.text
    # Another week waits behind the pending job instead of being refused.
    other_week = submit(client, "other-week", {**BODY, "run_at": "2026-08-17T00:00:00Z"})
    assert other_week.status_code == 202 and other_week.json()["status"] == "queued"
    page = client.get("/teams/checkout-api?job_id=" + row["job_id"])
    assert page.status_code == 200 and "Waiting for the worker" in page.text
    assert "connect-src 'self'" in page.headers["content-security-policy"]
    assert client.get("/teams/payments-api?job_id=" + row["job_id"]).status_code == 404
    with connect(CONFIG.sql, DB) as db:
        assert db.query_one("SELECT run_id FROM runs") is None
    token = client.get("/csrf").json()["token"]
    assert client.post("/runs", json=BODY, headers={"X-CSRF-Token": token}).status_code == 409


def test_submission_and_status_timestamps_preserve_sql_utc_instants(client: TestClient) -> None:
    posted = submit(client, "utc-timestamps")
    assert posted.status_code == 202, posted.text
    row = posted.json()

    def assert_instants(response: dict[str, Any], stored: dict[str, Any]) -> None:
        for key in ("submitted_at", "started_at", "completed_at"):
            if stored[key] is None:
                assert response[key] is None
            else:
                assert response[key].endswith("Z"), response[key]
                parsed = datetime.fromisoformat(response[key])
                assert parsed.utcoffset() == timedelta(0)
                assert parsed == stored[key].replace(tzinfo=UTC)

    with connect(CONFIG.sql, DB) as db:
        assert_instants(row, jobs.get_job(db, row["job_id"]))
        started = datetime(2026, 10, 13, 12, 0, 0, 123000)
        db.execute(
            "UPDATE run_jobs SET status='running',started_at=:at WHERE job_id=:id",
            {"id": row["job_id"], "at": started},
        )
        db.commit()
        running = client.get(row["status_url"])
        assert running.status_code == 200
        assert running.json()["started_at"] == "2026-10-13T12:00:00.123Z"
        assert_instants(running.json(), jobs.get_job(db, row["job_id"]))
        jobs.finish_job(db, row["job_id"], "failed", "Test interruption")
        finished = client.get(row["status_url"])
        assert finished.status_code == 200
        assert finished.json()["completed_at"] is not None
        assert_instants(finished.json(), jobs.get_job(db, row["job_id"]))


def test_installed_worker_completes_without_writing_files(
    client: TestClient, tmp_path: Path
) -> None:
    row = submit(client, "installed-worker").json()
    # Run from an empty directory; an absolute registry keeps the submitted registry hash.
    registry = str(Path("config/teams.json").resolve())
    worker = [sys.executable, "-m", "alerts_bi_admin", "run-worker", "--database", DB, "--once"]
    subprocess.run(
        [*worker, "--registry", registry],
        check=True,
        timeout=180,
        cwd=tmp_path,
    )
    row = client.get(row["status_url"]).json()
    assert row["status"] == "completed", row
    assert client.get(row["result_url"]).status_code == 200
    assert client.get(f"/runs/{row['run_id']}/scorecard.html").status_code == 422
    assert client.get(f"/runs/{row['run_id']}/daily_metrics.csv").status_code == 200
    assert not any(tmp_path.iterdir())
    with connect(CONFIG.sql, DB) as db:
        assert db.query_one("SELECT run_id FROM review_publications") is None


def test_lock_protects_live_worker_and_status_stays_readable(client: TestClient) -> None:
    row = submit(client, "live-lock").json()
    with connect(CONFIG.sql, DB) as db, jobs.execution_lock(db):
        db.execute(
            "UPDATE run_jobs SET status='running',started_at=:at WHERE job_id=:id",
            {"id": row["job_id"], "at": datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)},
        )
        db.commit()
        assert client.get(row["status_url"]).json()["status"] == "running"
        assert submit(client, "live-lock").json()["job_id"] == row["job_id"]
        blocked = submit(client, "blocked-live")
        assert blocked.status_code == 409
        assert "Already queued or running for this team" in blocked.text
        assert jobs.work_once(ApiSettings(CONFIG, database=DB)) is False
        assert jobs.get_job(db, row["job_id"])["status"] == "running"


def test_restart_does_not_mistake_older_result_for_interrupted_job(
    client: TestClient, settings: AdminSettings
) -> None:
    row = submit(client, "older-result").json()
    started = datetime.now(UTC).replace(tzinfo=None)
    with connect(CONFIG.sql, DB) as db:
        persist_run(
            db,
            PersistencePayload(
                run=sample_run(
                    run_id=row["run_id"],
                    started_at=started - timedelta(hours=1),
                    completed_at=started - timedelta(minutes=59),
                )
            ),
        )
        db.execute(
            "UPDATE run_jobs SET status='running',started_at=:at WHERE job_id=:id",
            {"id": row["job_id"], "at": started},
        )
        db.commit()
    assert jobs.work_once(ApiSettings(CONFIG, database=DB)) is False
    status = client.get(row["status_url"]).json()
    assert status["status"] == "failed" and "interrupted" in status["error_summary"]
    assert "Worker interrupted" in client.get("/teams/checkout-api").text


def test_restart_recognizes_commit_before_job_completion(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = submit(client, "commit-recovery").json()
    started = datetime(2026, 10, 13)
    with connect(CONFIG.sql, DB) as db:
        db.execute(
            "UPDATE run_jobs SET status='running',started_at=:at WHERE job_id=:id",
            {"id": row["job_id"], "at": started},
        )
        db.commit()
        persist_run(
            db,
            PersistencePayload(
                run=sample_run(
                    run_id=row["run_id"],
                    started_at=started + timedelta(seconds=1),
                    completed_at=started + timedelta(seconds=2),
                ),
                daily_metrics=[sample_daily(run_id=row["run_id"], alerts=31)],
            ),
        )
        expected = build_run_outputs(db, row["run_id"])
    # Recovery needs no registry, so it can run from an empty directory and leave it empty.
    monkeypatch.chdir(tmp_path)
    assert jobs.work_once(ApiSettings(CONFIG, database=DB)) is False
    assert not any(tmp_path.iterdir())
    status = client.get(row["status_url"]).json()
    assert status["status"] == "completed" and status["error_summary"] is None
    # Nothing is regenerated on recovery: each download renders the committed SQL.
    for name, content in expected.items():
        assert client.get(f"/runs/{row['run_id']}/{name}").content == content.encode("utf-8")


def test_pending_jobs_prevent_deletion_and_terminal_jobs_are_removed_with_run(
    client: TestClient, settings: AdminSettings
) -> None:
    row = submit(client, "pending-delete").json()
    # Seed the older result this pending rerun would replace; first runs have no parent to delete.
    with connect(CONFIG.sql, DB) as db:
        persist_run(db, PersistencePayload(run=sample_run(run_id=row["run_id"])))
    token = client.get("/csrf").json()["token"]
    response = client.post(
        f"/runs/{row['run_id']}/delete", data={"csrf": token, "confirm_delete": "1"}
    )
    assert response.status_code == 303 and "error=" in response.headers["location"]
    assert client.get(row["status_url"]).json()["status"] == "pending"
    assert jobs.work_once(ApiSettings(CONFIG, database=DB))
    response = client.post(
        f"/runs/{row['run_id']}/delete", data={"csrf": token, "confirm_delete": "1"}
    )
    assert response.status_code == 303 and "done=" in response.headers["location"]
    assert client.get(row["status_url"]).status_code == 404


def delete(client: TestClient, run_id: str) -> Any:
    token = client.get("/csrf").json()["token"]
    return client.post(f"/runs/{run_id}/delete", data={"csrf": token, "confirm_delete": "1"})


def present(run_id: str) -> bool:
    with connect(CONFIG.sql, DB) as db:
        return (
            db.query_one("SELECT 1 AS present FROM runs WHERE run_id=:id", {"id": run_id})
            is not None
        )


def test_a_failed_run_that_stored_nothing_is_deleted_to_run_with_new_model_settings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model audit refuses a changed batch size under the same run id until deletion."""
    first = submit(client, "settings-first").json()

    def fail_before_storing(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("injected failure after the model review")

    with monkeypatch.context() as patched:
        patched.setattr(service, "persist_run", fail_before_storing)
        assert jobs.work_once(ApiSettings(CONFIG, database=DB))
    assert client.get(first["status_url"]).json()["status"] == "failed"
    assert not present(first["run_id"])

    smaller = replace(CONFIG, llm=replace(CONFIG.llm, max_batch_size=50))
    second = submit(client, "settings-second").json()
    assert second["run_id"] == first["run_id"]
    assert jobs.work_once(ApiSettings(smaller, database=DB))
    refused = client.get(second["status_url"]).json()
    assert refused["status"] == "failed" and "Delete this run" in refused["error_summary"]
    page = client.get(f"/teams/checkout-api?job_id={second['job_id']}").text
    assert 'data-dialog="delete-run">Delete run' in page
    assert f'action="/runs/{first["run_id"]}/delete"' in page

    deleted = delete(client, first["run_id"])
    assert deleted.status_code == 303 and "done=" in deleted.headers["location"]
    params = {"id": first["run_id"]}
    with connect(CONFIG.sql, DB) as db:
        for table, column in (
            ("llm_review_scopes", "scope_id"),
            ("llm_review_batches", "scope_id"),
            ("run_jobs", "run_id"),
            ("run_analysis_checkpoints", "run_id"),
        ):
            assert db.query_one(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {column}=:id", params
            ) == {"n": 0}
        assert db.query_one("SELECT COUNT(*) AS n FROM llm_prompt_artifacts") != {"n": 0}
    assert delete(client, first["run_id"]).status_code == 404

    third = submit(client, "settings-third").json()
    assert jobs.work_once(ApiSettings(smaller, database=DB))
    assert client.get(third["status_url"]).json()["status"] == "completed"
    with connect(CONFIG.sql, DB) as db:
        scope = db.query_one(
            "SELECT settings_json FROM llm_review_scopes WHERE scope_id=:id", params
        )
    assert scope is not None and '"max_batch_size":50' in scope["settings_json"]


def test_a_failed_rerun_of_a_stored_week_offers_deleting_the_stored_run(
    client: TestClient,
) -> None:
    first = submit(client, "stored-first").json()
    assert jobs.work_once(ApiSettings(CONFIG, database=DB))
    assert present(first["run_id"])
    smaller = replace(CONFIG, llm=replace(CONFIG.llm, max_batch_size=50))
    rerun = submit(client, "stored-rerun").json()
    assert jobs.work_once(ApiSettings(smaller, database=DB))
    assert "Delete this run" in client.get(rerun["status_url"]).json()["error_summary"]
    page = client.get(f"/teams/checkout-api?job_id={rerun['job_id']}").text
    # The stored run's own dialog: its results go too.
    assert 'data-dialog="delete-run">Delete run' in page and "its results, publications" in page
    assert delete(client, first["run_id"]).status_code == 303 and not present(first["run_id"])
    again = submit(client, "stored-again").json()
    assert jobs.work_once(ApiSettings(smaller, database=DB))
    assert client.get(again["status_url"]).json()["status"] == "completed"


def test_refused_deletion_of_a_run_that_stored_nothing_returns_to_its_submission(
    client: TestClient,
) -> None:
    row = submit(client, "pending-unstored").json()
    response = delete(client, row["run_id"])
    assert response.status_code == 303 and "error=" in response.headers["location"]
    assert f"job_id={row['job_id']}" in response.headers["location"]
    assert client.get(row["status_url"]).json()["status"] == "pending"


def test_pending_job_does_not_block_deleting_another_run(client: TestClient) -> None:
    row = submit(client, "pending-other").json()
    other = "d" * 64
    with connect(CONFIG.sql, DB) as db:
        persist_run(db, PersistencePayload(run=sample_run(run_id=other)))
    response = delete(client, other)
    assert response.status_code == 303 and "done=" in response.headers["location"]
    assert not present(other)
    assert client.get(row["status_url"]).json()["status"] == "pending"


def test_running_job_does_not_block_deleting_another_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker holds the shared execution lock for the whole job; only its own run waits."""
    row = submit(client, "running-other").json()
    other = "d" * 64
    with connect(CONFIG.sql, DB) as db:
        # The older result this rerun replaces, and an unrelated stored run.
        persist_run(db, PersistencePayload(run=sample_run(run_id=row["run_id"])))
        persist_run(db, PersistencePayload(run=sample_run(run_id=other)))
    entered, release = Event(), Event()
    actual = service.execute_request

    def delayed(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(30), "The test must release the deliberately delayed worker"
        return actual(*args, **kwargs)

    monkeypatch.setattr(jobs, "execute_request", delayed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(jobs.work_once, ApiSettings(CONFIG, database=DB))
        try:
            assert entered.wait(10)
            assert client.get(row["status_url"]).json()["status"] == "running"
            own = delete(client, row["run_id"])
            assert own.status_code == 303 and "error=" in own.headers["location"]
            assert present(row["run_id"])
            deleted = delete(client, other)
            assert deleted.status_code == 303 and "done=" in deleted.headers["location"]
            assert not present(other)
        finally:
            release.set()
        assert future.result(timeout=120)
    assert client.get(row["status_url"]).json()["status"] == "completed"
    assert present(row["run_id"])


@pytest.mark.parametrize("state", ["pending", "running"])
@pytest.mark.parametrize("command", ["run", "weekly", "verify-acceptance", "report"])
def test_orphan_blocks_installed_writers_until_recovery(
    client: TestClient, tmp_path: Path, command: str, state: str
) -> None:
    row = submit(client, "orphan-before-persist-" + command).json()
    # The worker claimed the job, then died without committing analysis. Its SQL
    # locks are gone, but the durable active slot must still reserve the result.
    if state == "running":
        with connect(CONFIG.sql, DB) as db:
            db.execute(
                "UPDATE run_jobs SET status='running',started_at=:at WHERE job_id=:id",
                {"id": row["job_id"], "at": datetime.now(UTC).replace(tzinfo=None)},
            )
            db.commit()
    arguments = [sys.executable, "-m", "alerts_bi_admin", command, "--database", DB]
    if command == "run":
        arguments += ["--team", BODY["team"], "--run-at", BODY["run_at"], "--fake-llm"]
    elif command == "weekly":
        arguments += ["--as-of", "2026-08-25T18:00:00Z", "--fake-llm"]
    elif command == "report":
        arguments += ["--run-id", row["run_id"]]
    arguments += ["--out", str(tmp_path / command)]
    refused = subprocess.run(arguments, capture_output=True, text=True, timeout=180)
    assert refused.returncode != 0, refused.stdout
    assert "manual job" in refused.stderr, refused.stderr
    with connect(CONFIG.sql, DB) as db:
        assert db.query_one("SELECT run_id FROM runs") is None
        assert db.query_one("SELECT log_id FROM weekly_review_log") is None
    api = ApiSettings(CONFIG, database=DB)
    assert jobs.work_once(api) is (state == "pending")
    status = client.get(row["status_url"]).json()
    if state == "pending":
        assert status["status"] == "completed" and status["error_summary"] is None
    else:
        assert status["status"] == "failed" and "interrupted" in status["error_summary"]
    if command == "run" and state == "running":
        # Reconciliation releases both the durable reservation and SQL locks.
        retried = subprocess.run(arguments, capture_output=True, text=True, timeout=180)
        assert retried.returncode == 0, retried.stderr
        with connect(CONFIG.sql, DB) as db:
            assert db.query_one("SELECT run_id FROM runs") == {"run_id": row["run_id"]}
        assert client.get(row["status_url"]).json()["status"] == "failed"


def test_execution_failure_is_visible_and_releases_slot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = submit(client, "execution-failure").json()

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Test failure")

    monkeypatch.setattr(jobs, "execute_request", fail)
    assert jobs.work_once(ApiSettings(CONFIG, database=DB))
    status = client.get(row["status_url"]).json()
    assert status["status"] == "failed" and status["result_url"] is None
    assert "Analysis failed" in client.get("/teams/checkout-api").text
    with connect(CONFIG.sql, DB) as db:
        assert db.query_one("SELECT job_id FROM run_jobs WHERE active_slot=1") is None


def test_changed_registry_is_refused_before_execution(
    settings: AdminSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "teams.json"
    registry.write_bytes(Path("config/teams.json").read_bytes())
    api = ApiSettings(CONFIG, database=DB, registry_path=str(registry))
    row = jobs.submit_job(api, RunRequest.model_validate(BODY), uuid4().hex)
    registry.write_bytes(registry.read_bytes() + b"\n")

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Changed inputs must be refused before running the engine")

    monkeypatch.setattr(jobs, "execute_request", unexpected)
    assert jobs.work_once(api)
    with connect(CONFIG.sql, DB) as db:
        assert jobs.get_job(db, row["job_id"])["status"] == "failed"


def test_model_mode_drift_is_refused_before_elasticsearch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = submit(client, "model-drift").json()
    with connect(CONFIG.sql, DB) as db:
        db.execute("UPDATE run_jobs SET llm_mode='off' WHERE job_id=:id", {"id": row["job_id"]})
        db.commit()

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Changed run identity must be refused before reading alerts")

    monkeypatch.setattr("src.engine.run.orchestrator.analyze_team", unexpected)
    assert jobs.work_once(ApiSettings(CONFIG, database=DB))
    assert client.get(row["status_url"]).json()["status"] == "failed"


def test_failure_after_commit_completes_without_a_warning(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = submit(client, "post-commit-failure").json()

    def persist_then_fail(*args: Any, **kwargs: Any) -> None:
        persist_run(*args, **kwargs)
        raise RuntimeError("Test failure after commit")

    monkeypatch.setattr(service, "persist_run", persist_then_fail)
    assert jobs.work_once(ApiSettings(CONFIG, database=DB))
    status = client.get(row["status_url"]).json()
    assert status["status"] == "completed" and status["error_summary"] is None
    assert client.get(status["result_url"]).status_code == 200
    assert client.get(f"/runs/{row['run_id']}/daily_metrics.csv").status_code == 200


def test_slow_worker_keeps_status_and_navigation_responsive(
    client: TestClient, settings: AdminSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = submit(client, "slow-worker", {**BODY, "run_at": "2026-08-17T00:00:00Z"}).json()
    entered, release = Event(), Event()
    actual = service.execute_request

    def delayed(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(30), "The test must release the deliberately delayed worker"
        return actual(*args, **kwargs)

    monkeypatch.setattr(jobs, "execute_request", delayed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(jobs.work_once, ApiSettings(CONFIG, database=DB))
        try:
            assert entered.wait(10)
            assert client.get(row["status_url"]).json()["status"] == "running"
            assert client.get("/teams/checkout-api").status_code == 200
            assert client.get("/").status_code == 200
            assert (
                submit(client, "slow-worker", {**BODY, "run_at": "2026-08-17T00:00:00Z"}).json()[
                    "job_id"
                ]
                == row["job_id"]
            )
            # Another week waits its turn instead of being refused (since 2026-10-26).
            queued = submit(client, "busy-slow-worker")
            assert queued.status_code == 202 and queued.json()["status"] == "queued"
        finally:
            release.set()
        assert future.result(timeout=120)
    assert client.get(row["status_url"]).json()["status"] == "completed"


@pytest.mark.parametrize(
    "first_key,second_key", [("Attempt-A", "attempt-a"), ("padded", "padded ")]
)
def test_distinct_exact_keys_create_new_attempts(
    client: TestClient,
    settings: AdminSettings,
    monkeypatch: pytest.MonkeyPatch,
    first_key: str,
    second_key: str,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Finish the attempt without running analysis")

    monkeypatch.setattr(jobs, "execute_request", fail)
    api = ApiSettings(CONFIG, database=DB)
    first = submit(client, first_key)
    assert first.status_code == 202, first.text
    assert jobs.work_once(api)
    second = submit(client, second_key)
    assert second.status_code == 202, second.text
    assert second.json()["job_id"] != first.json()["job_id"]
    assert submit(client, first_key).json()["job_id"] == first.json()["job_id"]
    assert submit(client, second_key).json()["job_id"] == second.json()["job_id"]
    with connect(CONFIG.sql, DB) as db:
        # SQL's ordinary string comparison would conflate these under CI collation.
        assert db.query_one(
            "SELECT CASE WHEN :first COLLATE Latin1_General_100_CI_AS = :second THEN 1 ELSE 0 END AS same",
            {"first": first_key, "second": second_key},
        ) == {"same": 1}
        first_row = jobs.get_job(db, first.json()["job_id"])
        second_row = jobs.get_job(db, second.json()["job_id"])
        assert first_row["idempotency_key"] == first_key
        assert second_row["idempotency_key"] == second_key
        assert first_row["idempotency_key_hash"] != second_row["idempotency_key_hash"]
    assert jobs.work_once(api)


def test_idle_worker_never_takes_execution_locks(
    settings: AdminSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("An empty queue must not contend for any execution lock")

    monkeypatch.setattr(jobs, "execution_lock", unexpected)
    assert jobs.work_once(ApiSettings(CONFIG, database=DB)) is False


def test_job_status_follows_operator_wide_access_model(client: TestClient) -> None:
    row = submit(client, "operator-access").json()
    second = client.get(row["status_url"])
    assert second.status_code == 200 and second.json()["team_id"] == "checkout-api"
    assert "requested_by" not in second.json() and "idempotency_key" not in second.json()
    assert client.get("/teams/payments-api?job_id=" + row["job_id"]).status_code == 404
