"""SQL-backed submissions and one manual-run worker, independent of HTTP requests.

The session lock lives on the execution connection, surviving journal/persistence commits.
A new worker can recover running jobs only after the previous session released its lock.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from alerts_bi_operations.registry import RegistryError, load_registry, select_team
from alerts_bi_shared.db.connection import Database, connect
from alerts_bi_shared.hashing import sha256_text
from alerts_bi_shared.logging_setup import log, redact_error
from fastapi import HTTPException

from ..config import ApiSettings
from ..run.orchestrator import compute_run_id
from ..run.outputs import render_run_report
from ..weekly.runner import WeeklyBusy, schedule_lock
from .schemas import RunRequest
from .service import close_client, execute_request, latest_week_end, select_client

LOCK_RESOURCE = "alerts-bi-manual-run"
EXPORT_WARNING = "Analysis saved; CSV export failed. Regenerate exports with the report command."


@contextmanager
def execution_lock(db: Database, settings: ApiSettings | None = None) -> Iterator[None]:
    # The existing scheduler lock lives on its own connection, protecting the complete
    # lifecycle from CLI/report/deletion while the data session commits model audit.
    try:
        with (
            schedule_lock(settings.config, settings.target_database) if settings else nullcontext()
        ):
            row = db.query_one(
                "SET NOCOUNT ON; DECLARE @result INT; "
                "EXEC @result = sp_getapplock @Resource=:resource, @LockMode='Exclusive', "
                "@LockOwner='Session', @LockTimeout=0; SELECT @result AS result",
                {"resource": LOCK_RESOURCE},
            )
            if row is None or int(row["result"]) < 0:
                raise HTTPException(
                    409, "A run is already in progress. Try again after it finishes."
                )
            try:
                yield
            finally:
                db.execute(
                    "EXEC sp_releaseapplock @Resource=:resource, @LockOwner='Session'",
                    {"resource": LOCK_RESOURCE},
                )
                db.commit()
    except WeeklyBusy:
        raise HTTPException(
            409, "A run, report, weekly schedule or deletion is active. Try again when it finishes."
        ) from None


def get_job(db: Database, job_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM run_jobs WHERE job_id=:id", {"id": job_id})
    if row is None:
        raise HTTPException(404, "No such run submission.")
    return row


def team_jobs(db: Database, team_id: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT TOP 20 * FROM run_jobs WHERE team_id=:team AND (status <> 'completed' OR error_summary IS NOT NULL) "
        "ORDER BY submitted_at DESC, job_id DESC",
        {"team": team_id},
    )


def refuse_active_job(db: Database) -> None:
    if db.query_one("SELECT TOP 1 job_id FROM run_jobs WHERE active_slot=1"):
        raise HTTPException(
            409, "A run is already pending or running. Try again after it finishes."
        )


def submit_job(
    settings: ApiSettings, body: RunRequest, operator: str, idempotency_key: str
) -> dict[str, Any]:
    if not 1 <= len(idempotency_key) <= 64 or not idempotency_key.isascii():
        raise HTTPException(400, "Supply an ASCII Idempotency-Key of 1 to 64 characters.")
    if not operator or len(operator) > 256:
        raise HTTPException(400, "The operator identity is invalid.")
    request_hash = sha256_text(body.model_dump_json())
    # Binary identity preserves case and trailing spaces regardless of SQL collation.
    key_hash = bytes.fromhex(sha256_text(idempotency_key))
    with connect(settings.config.sql, settings.target_database) as db:
        existing = db.query_one(
            "SELECT * FROM run_jobs WHERE requested_by=:operator AND idempotency_key_hash=:key_hash",
            {"operator": operator, "key_hash": key_hash},
        )
        if existing:
            if existing["request_hash"] != request_hash:
                raise HTTPException(
                    409, "This submission key was already used with different options."
                )
            return existing
        with execution_lock(db, settings):
            # Recheck after acquiring the lock: a simultaneous identical submission may
            # have committed since the first lookup.
            existing = db.query_one(
                "SELECT * FROM run_jobs WHERE requested_by=:operator AND idempotency_key_hash=:key_hash",
                {"operator": operator, "key_hash": key_hash},
            )
            if existing:
                if existing["request_hash"] != request_hash:
                    raise HTTPException(
                        409, "This submission key was already used with different options."
                    )
                return existing
            refuse_active_job(db)
            try:
                registry = (
                    load_registry(settings.registry_path)
                    if settings.registry_path
                    else load_registry()
                )
                select_team(registry, body.team)
            except RegistryError as exc:
                raise HTTPException(400, str(exc)) from None
            run_at = body.run_at or latest_week_end(datetime.now(UTC))
            run_at = run_at.replace(tzinfo=UTC) if run_at.tzinfo is None else run_at.astimezone(UTC)
            client, _ = select_client(settings.config, body.llm)
            model_version = client.model_version if client else None
            close_client(client)
            from alerts_bi_shared.window import build_run_window

            window = build_run_window(run_at)
            run_id = compute_run_id(
                team_id=body.team,
                run_at=window.run_at,
                window_start=window.window_start,
                registry_sha256=registry.file_sha256,
                model_version=model_version,
            )
            if db.query_one(
                "SELECT run_id FROM review_publications WHERE run_id=:id AND withdrawn_at IS NULL",
                {"id": run_id},
            ):
                raise HTTPException(
                    409, "This run is published. Withdraw it before running it again."
                )
            job_id = uuid4().hex
            db.execute(
                "INSERT INTO run_jobs (job_id,idempotency_key,idempotency_key_hash,requested_by,request_hash,team_id,"
                "run_at,llm_mode,registry_sha256,run_id,status,active_slot,submitted_at) "
                "VALUES (:id,:key,:key_hash,:operator,:hash,:team,:at,:mode,:registry,:run,'pending',1,:now)",
                {
                    "id": job_id,
                    "key": idempotency_key,
                    "key_hash": key_hash,
                    "operator": operator,
                    "hash": request_hash,
                    "team": body.team,
                    "at": run_at.replace(tzinfo=None),
                    "mode": body.llm,
                    "registry": registry.file_sha256,
                    "run": run_id,
                    "now": datetime.now(UTC).replace(tzinfo=None),
                },
            )
            db.commit()
            log.info(
                "run.job_submitted",
                job_id=job_id,
                team_id=body.team,
                run_id=run_id,
                operator=operator,
            )
            return get_job(db, job_id)


def finish_job(db: Database, job_id: str, state: str, error: str | None = None) -> None:
    db.execute(
        "UPDATE run_jobs SET status=:state,active_slot=NULL,completed_at=:now,error_summary=:error "
        "WHERE job_id=:id AND status='running'",
        {
            "id": job_id,
            "state": state,
            "now": datetime.now(UTC).replace(tzinfo=None),
            "error": error,
        },
    )
    db.commit()
    log.info(
        "run.job_finished",
        job_id=job_id,
        status=state,
        export_warning=state == "completed" and error is not None,
    )


def committed_result(db: Database, job: dict[str, Any]) -> bool:
    # An older execution with the same deterministic ID is not this job's result.
    return (
        db.query_one(
            "SELECT run_id FROM runs WHERE run_id=:id AND status='completed' AND started_at>=:started",
            {"id": job["run_id"], "started": job["started_at"]},
        )
        is not None
    )


def work_once(settings: ApiSettings) -> bool:
    """Claim at most one job, recover orphans, then run on the lock-owning SQL session."""
    with connect(settings.config.sql, settings.target_database) as db:
        # An idle poll must not compete with submissions, reports or weekly execution.
        # Recheck and claim under the locks below; a new job after this read waits one poll.
        if db.query_one("SELECT TOP 1 job_id FROM run_jobs WHERE active_slot=1") is None:
            return False
        try:
            with execution_lock(db, settings):
                for orphan in db.query("SELECT * FROM run_jobs WHERE status='running'"):
                    completed = committed_result(db, orphan)
                    error: str | None = "Worker interrupted. Start a new run to retry."
                    if completed:
                        try:
                            # The prior worker may have died between SQL commit and the
                            # final CSV write. Rebuild every export before clearing the job.
                            render_run_report(
                                db,
                                str(orphan["run_id"]),
                                settings.out_root / str(orphan["run_id"])[:16],
                            )
                            error = None
                        except Exception as exc:
                            db.rollback()
                            log.error(
                                "run.export_recovery_failed",
                                job_id=orphan["job_id"],
                                error=redact_error(exc),
                            )
                            error = EXPORT_WARNING
                    finish_job(
                        db,
                        str(orphan["job_id"]),
                        "completed" if completed else "failed",
                        error,
                    )
                job = db.query_one(
                    "SELECT TOP 1 * FROM run_jobs WHERE status='pending' ORDER BY submitted_at"
                )
                if job is None:
                    return False
                started = datetime.now(UTC).replace(tzinfo=None)
                started = started.replace(microsecond=started.microsecond // 1000 * 1000)
                db.execute(
                    "UPDATE run_jobs SET status='running',started_at=:at WHERE job_id=:id",
                    {"id": job["job_id"], "at": started},
                )
                db.commit()
                job["started_at"] = started
                log.info(
                    "run.job_started",
                    job_id=job["job_id"],
                    team_id=job["team_id"],
                    run_id=job["run_id"],
                )
                try:
                    registry = (
                        load_registry(settings.registry_path)
                        if settings.registry_path
                        else load_registry()
                    )
                    if registry.file_sha256 != job["registry_sha256"]:
                        raise ValueError("Registry changed since submission. Start a new run.")
                    body = RunRequest(
                        team=job["team_id"], run_at=job["run_at"], llm=job["llm_mode"]
                    )
                    execute_request(settings, db, body, expected_run_id=str(job["run_id"]))
                    finish_job(db, str(job["job_id"]), "completed")
                except Exception as exc:
                    db.rollback()
                    completed = committed_result(db, job)
                    log.error("run.job_failed", job_id=job["job_id"], error=redact_error(exc))
                    finish_job(
                        db,
                        str(job["job_id"]),
                        "completed" if completed else "failed",
                        EXPORT_WARNING
                        if completed
                        else "Analysis failed. Check the service logs, then start a new run to retry.",
                    )
                return True
        except HTTPException as exc:
            if exc.status_code == 409:
                return False
            raise


def run_worker(settings: ApiSettings, *, once: bool = False, poll_seconds: float = 2) -> None:
    """The supervisor restarts a worker after infrastructure errors; jobs stay durable."""
    while True:
        worked = work_once(settings)
        if once:
            return
        if not worked:
            time.sleep(poll_seconds)
