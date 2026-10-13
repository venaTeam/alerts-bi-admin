"""Durable submission/worker/recovery behavior on real SQL Server and Elasticsearch."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import pytest
from alerts_bi_shared.db.connection import connect
from fastapi.testclient import TestClient
from src.app import build_admin
from src.config import AdminConfig, AdminSettings
from src.engine.api import jobs, service
from src.engine.api.schemas import RunRequest
from src.engine.config import ApiSettings, load_config
from src.engine.db.migrate import reset_test_database
from src.engine.db.repositories import PersistencePayload, persist_run

from tests.helpers.sql import sample_run

pytestmark = pytest.mark.integration
CONFIG = load_config()
DB = CONFIG.sql.test_database
BODY = {"team": "checkout-api", "run_at": "2026-08-25T18:00:00Z", "llm": "fake"}


@pytest.fixture(scope="module")
def settings(tmp_path_factory: pytest.TempPathFactory) -> AdminSettings:
    # Fail migration regressions when SQL is reachable instead of hiding them as skips.
    try:
        with connect(CONFIG.sql, "master") as db:
            db.query("SELECT 1 AS ok")
    except Exception as exc:
        pytest.skip(f"Local SQL Server unavailable: {exc}")
    reset_test_database(CONFIG.sql, DB)
    return AdminSettings(
        AdminConfig(CONFIG.sql), DB, "j" * 40, out_root=tmp_path_factory.mktemp("job-outputs")
    )


@pytest.fixture
def client(settings: AdminSettings) -> Iterator[TestClient]:
    with TestClient(
        build_admin(settings), headers={"X-Forwarded-User": "job-tester"}, follow_redirects=False
    ) as value:
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
    assert submit(client, "another").status_code == 409
    page = client.get("/teams/checkout-api?job_id=" + row["job_id"])
    assert page.status_code == 200 and "Waiting for the worker" in page.text
    assert "connect-src 'self'" in page.headers["content-security-policy"]
    assert client.get("/teams/payments-api?job_id=" + row["job_id"]).status_code == 404
    with connect(CONFIG.sql, DB) as db:
        assert db.query_one("SELECT run_id FROM runs") is None
    token = client.get("/csrf").json()["token"]
    assert client.post("/runs", json=BODY, headers={"X-CSRF-Token": token}).status_code == 409


def test_installed_worker_completes_and_outputs_have_no_html(
    client: TestClient, settings: AdminSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADMIN_OUT_DIR", str(settings.out_root))
    subprocess.run(
        [sys.executable, "-m", "alerts_bi_admin", "run-worker", "--database", DB, "--once"],
        check=True,
        timeout=180,
    )
    row = submit(client, "accepted").json()
    assert row["status"] == "completed", row
    assert client.get(row["result_url"]).status_code == 200
    assert client.get(f"/runs/{row['run_id']}/scorecard.html").status_code == 200
    directory = settings.out_root / row["run_id"][:16]
    assert {p.name for p in directory.iterdir()} == {
        "daily_metrics.csv",
        "rule_counts.csv",
        "alert_worklist.csv",
    }
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
        assert submit(client, "blocked-live").status_code == 409
        assert jobs.work_once(ApiSettings(CONFIG, database=DB)) is False
        assert jobs.get_job(db, row["job_id"])["status"] == "running"


def test_restart_does_not_mistake_older_result_for_interrupted_job(client: TestClient) -> None:
    # The prior completed run has the same deterministic ID, but predates this attempt.
    assert jobs.work_once(ApiSettings(CONFIG, database=DB)) is False
    row = submit(client, "live-lock").json()
    assert row["status"] == "failed" and "interrupted" in row["error_summary"]
    page = client.get("/teams/checkout-api")
    assert "Worker interrupted" in page.text


def test_restart_recognizes_commit_before_job_completion(client: TestClient) -> None:
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
                )
            ),
        )
    assert jobs.work_once(ApiSettings(CONFIG, database=DB)) is False
    assert client.get(row["status_url"]).json()["status"] == "completed"


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
    api = ApiSettings(CONFIG, database=DB, registry_path=str(registry), out_root=settings.out_root)
    row = jobs.submit_job(api, RunRequest.model_validate(BODY), "job-tester", uuid4().hex)
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


def test_export_failure_preserves_committed_analysis_and_shows_warning(
    client: TestClient, settings: AdminSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = submit(client, "export-failure").json()

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise OSError("Test export failure")

    monkeypatch.setattr(service, "render_run_report", fail)
    assert jobs.work_once(ApiSettings(CONFIG, database=DB, out_root=settings.out_root))
    status = client.get(row["status_url"]).json()
    assert status["status"] == "completed" and "CSV export failed" in status["error_summary"]
    assert client.get(status["result_url"]).status_code == 200
    assert "CSV export failed" in client.get("/teams/checkout-api").text


def test_slow_worker_keeps_status_and_navigation_responsive(
    client: TestClient, settings: AdminSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = submit(client, "slow-worker", {**BODY, "run_at": "2026-08-25T18:03:00Z"}).json()
    entered, release = Event(), Event()
    actual = service.execute_request

    def delayed(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(30), "The test must release the deliberately delayed worker"
        return actual(*args, **kwargs)

    monkeypatch.setattr(jobs, "execute_request", delayed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            jobs.work_once, ApiSettings(CONFIG, database=DB, out_root=settings.out_root)
        )
        try:
            assert entered.wait(10)
            assert client.get(row["status_url"]).json()["status"] == "running"
            assert client.get("/teams/checkout-api").status_code == 200
            assert client.get("/").status_code == 200
            assert (
                submit(client, "slow-worker", {**BODY, "run_at": "2026-08-25T18:03:00Z"}).json()[
                    "job_id"
                ]
                == row["job_id"]
            )
            assert submit(client, "busy-slow-worker").status_code == 409
        finally:
            release.set()
        assert future.result(timeout=120)
    assert client.get(row["status_url"]).json()["status"] == "completed"
