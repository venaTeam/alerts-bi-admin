"""SQL-backed submissions and one manual-run worker, independent of HTTP requests.

The session lock lives on the execution connection, surviving journal/persistence commits.
A new worker can recover running jobs only after the previous session released its lock.
Queued jobs (backfill weeks, and manual runs accepted while a backfill is in progress) hold
no slot; the worker claims them one at a time after pending work (docs/backfill.md).
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
from ..weekly.runner import (
    BACKFILL_YIELD_RESOURCE,
    WeeklyBusy,
    backfill_in_progress,
    schedule_lock,
)
from .schemas import RunRequest
from .service import close_client, execute_request, latest_week_end, select_client

LOCK_RESOURCE = "alerts-bi-manual-run"
ACTIVE_STATES = ("queued", "pending", "running")
QUEUE_LOCK_PREFIX = "alerts-bi-queue:"


class ExecutionBusy(HTTPException):
    """The execution lock or the active slot is taken; other 409 refusals stay distinct."""

    def __init__(self, detail: str) -> None:
        super().__init__(409, detail)


@contextmanager
def execution_lock(db: Database, settings: ApiSettings | None = None) -> Iterator[None]:
    # The existing scheduler lock lives on its own connection, protecting the complete
    # lifecycle from CLI/report/deletion while the data session commits model audit.
    try:
        with (
            schedule_lock(settings.config, settings.target_database, allow_manual_job=True)
            if settings
            else nullcontext()
        ):
            row = db.query_one(
                "SET NOCOUNT ON; DECLARE @result INT; "
                "EXEC @result = sp_getapplock @Resource=:resource, @LockMode='Exclusive', "
                "@LockOwner='Session', @LockTimeout=0; SELECT @result AS result",
                {"resource": LOCK_RESOURCE},
            )
            if row is None or int(row["result"]) < 0:
                raise ExecutionBusy("A run is already in progress. Try again after it finishes.")
            try:
                yield
            finally:
                db.execute(
                    "EXEC sp_releaseapplock @Resource=:resource, @LockOwner='Session'",
                    {"resource": LOCK_RESOURCE},
                )
                db.commit()
    except WeeklyBusy:
        raise ExecutionBusy(
            "A run, report, weekly schedule or deletion is active. Try again when it finishes."
        ) from None


def get_job(db: Database, job_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM run_jobs WHERE job_id=:id", {"id": job_id})
    if row is None:
        raise HTTPException(404, "No such run submission.")
    return row


def team_jobs(db: Database, team_id: str) -> list[dict[str, Any]]:
    """Manual submissions needing attention; backfill weeks have their own panel."""
    return db.query(
        "SELECT TOP 20 * FROM run_jobs WHERE team_id=:team AND backfill_id IS NULL "
        "AND status <> 'cancelled' AND (status <> 'completed' OR error_summary IS NOT NULL) "
        "ORDER BY submitted_at DESC, job_id DESC",
        {"team": team_id},
    )


def lock_team_queue(db: Database, team_id: str, timeout_ms: int = 10000) -> bool:
    """Hold the team's queue lock until the current transaction ends.

    Every run job insert and run deletion takes it, so a deletion cannot remove a queued
    week or run accepted after its own checks (backfill admission bypasses the execution lock).
    """
    row = db.query_one(
        "SET NOCOUNT ON; DECLARE @result INT; "
        "EXEC @result = sp_getapplock @Resource=:resource, @LockMode='Exclusive', "
        "@LockOwner='Transaction', @LockTimeout=:timeout; SELECT @result AS result",
        {"resource": (QUEUE_LOCK_PREFIX + team_id)[:255], "timeout": timeout_ms},
    )
    return row is not None and int(row["result"]) >= 0


#: Who requested a submission is not recorded (design "Admin console without login",
#: 2026-10-19). The NOT NULL column holds this empty value, so submission keys are unique
#: across every client, not per person.
NO_REQUESTER = ""


def refuse_active_job(db: Database) -> None:
    if db.query_one("SELECT TOP 1 job_id FROM run_jobs WHERE active_slot=1"):
        raise ExecutionBusy("A run is already pending or running. Try again after it finishes.")


def matching_submission(
    rows: list[dict[str, Any]], request_hash: str, conflict: str
) -> dict[str, Any] | None:
    """The one stored submission that a retry with this key and request returns, if any.

    Migration 019 kept a key that several people had used as one ``#<id>`` row each, so a key
    can match several rows: a retry gets the one with its own options, and refuses when that
    is still more than one.
    """
    if not rows:
        return None
    same = [row for row in rows if row["request_hash"] == request_hash]
    if not same:
        raise HTTPException(409, conflict)
    if len(same) > 1:
        raise HTTPException(
            409,
            "Several earlier submissions used this key. Reload the page, or use a new "
            "Idempotency-Key, and submit again.",
        )
    return same[0]


def _existing_submission(db: Database, key_hash: bytes, request_hash: str) -> dict[str, Any] | None:
    rows = db.query(
        "SELECT * FROM run_jobs WHERE idempotency_key_hash=:key_hash", {"key_hash": key_hash}
    )
    return matching_submission(
        rows, request_hash, "This submission key was already used with different options."
    )


def _admit(
    settings: ApiSettings,
    db: Database,
    body: RunRequest,
    idempotency_key: str,
    key_hash: bytes,
    request_hash: str,
    status: str,
) -> dict[str, Any]:
    """Validate the team, capture the run identity and store one submission row."""
    try:
        registry = (
            load_registry(settings.registry_path) if settings.registry_path else load_registry()
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
    if not lock_team_queue(db, body.team):
        raise HTTPException(409, "This team's runs are being changed. Try again.")
    if db.query_one(
        "SELECT run_id FROM review_publications WHERE run_id=:id AND withdrawn_at IS NULL",
        {"id": run_id},
    ):
        raise HTTPException(409, "This run is published. Withdraw it before running it again.")
    job_id = uuid4().hex
    db.execute(
        "INSERT INTO run_jobs (job_id,idempotency_key,idempotency_key_hash,requested_by,request_hash,team_id,"
        "run_at,llm_mode,registry_sha256,run_id,status,active_slot,submitted_at) "
        "VALUES (:id,:key,:key_hash,:requester,:hash,:team,:at,:mode,:registry,:run,:status,:slot,:now)",
        {
            "id": job_id,
            "key": idempotency_key,
            "key_hash": key_hash,
            "requester": NO_REQUESTER,
            "hash": request_hash,
            "team": body.team,
            "at": run_at.replace(tzinfo=None),
            "mode": body.llm,
            "registry": registry.file_sha256,
            "run": run_id,
            "status": status,
            "slot": 1 if status == "pending" else None,
            "now": datetime.now(UTC).replace(tzinfo=None),
        },
    )
    db.commit()
    log.info(
        "run.job_submitted",
        job_id=job_id,
        team_id=body.team,
        run_id=run_id,
        status=status,
    )
    return get_job(db, job_id)


def submit_job(settings: ApiSettings, body: RunRequest, idempotency_key: str) -> dict[str, Any]:
    if not 1 <= len(idempotency_key) <= 64 or not idempotency_key.isascii():
        raise HTTPException(400, "Supply an ASCII Idempotency-Key of 1 to 64 characters.")
    request_hash = sha256_text(body.model_dump_json())
    # Binary identity preserves case and trailing spaces regardless of SQL collation.
    key_hash = bytes.fromhex(sha256_text(idempotency_key))
    with connect(settings.config.sql, settings.target_database) as db:
        existing = _existing_submission(db, key_hash, request_hash)
        if existing:
            return existing
        try:
            with execution_lock(db, settings):
                # Recheck after acquiring the lock: a simultaneous identical submission may
                # have committed since the first lookup.
                existing = _existing_submission(db, key_hash, request_hash)
                if existing:
                    return existing
                status = "pending"
                try:
                    refuse_active_job(db)
                except ExecutionBusy:
                    if not backfill_in_progress(db):
                        raise
                    status = "queued"
                return _admit(settings, db, body, idempotency_key, key_hash, request_hash, status)
        except ExecutionBusy:
            db.rollback()
            if not backfill_in_progress(db):
                raise
        # A backfill week holds the execution lock. Queue this run ahead of the remaining
        # weeks; a queued row holds no slot, so admission needs no execution lock.
        try:
            return _admit(settings, db, body, idempotency_key, key_hash, request_hash, "queued")
        except Exception:
            db.rollback()
            # A simultaneous identical submission wins the unique key; return its row.
            existing = _existing_submission(db, key_hash, request_hash)
            if existing:
                return existing
            raise


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
    log.info("run.job_finished", job_id=job_id, status=state)


def committed_result(db: Database, job: dict[str, Any]) -> bool:
    # An older execution with the same deterministic ID is not this job's result.
    return (
        db.query_one(
            "SELECT run_id FROM runs WHERE run_id=:id AND status='completed' AND started_at>=:started",
            {"id": job["run_id"], "started": job["started_at"]},
        )
        is not None
    )


def _may_claim_backfill(db: Database) -> bool:
    """False while a weekly invocation waits for the current backfill week to finish."""
    row = db.query_one(
        "SELECT APPLOCK_TEST('public', :resource, 'Shared', 'Session') AS free",
        {"resource": BACKFILL_YIELD_RESOURCE},
    )
    return row is not None and int(row["free"]) == 1


def _has_work(db: Database) -> bool:
    if db.query_one(
        "SELECT TOP 1 job_id FROM run_jobs WHERE active_slot=1 "
        "OR (status='queued' AND backfill_id IS NULL)"
    ):
        return True
    queued = db.query_one(
        "SELECT TOP 1 job_id FROM run_jobs WHERE status='queued' AND backfill_id IS NOT NULL"
    )
    return queued is not None and _may_claim_backfill(db)


def claim_queued(db: Database, started: datetime) -> dict[str, Any] | None:
    """Claim the next queued job: manual runs first, then backfill weeks oldest first."""
    candidates = db.query(
        "SELECT TOP 5 j.job_id FROM run_jobs j "
        "LEFT JOIN run_backfills b ON b.backfill_id=j.backfill_id "
        "WHERE j.status='queued' AND (j.backfill_id IS NULL OR :backfill=1) "
        # Manual runs keep submission order; backfill weeks go by backfill, then oldest week.
        "ORDER BY CASE WHEN j.backfill_id IS NULL THEN 0 ELSE 1 END, "
        "CASE WHEN j.backfill_id IS NULL THEN j.submitted_at END, b.submitted_at, "
        "j.run_at, j.submitted_at, j.job_id",
        {"backfill": 1 if _may_claim_backfill(db) else 0},
    )
    for candidate in candidates:
        # A cancellation may have committed since the read; claim only a still-queued row.
        claimed = db.query(
            "UPDATE run_jobs SET status='running',active_slot=1,started_at=:at "
            "OUTPUT inserted.job_id WHERE job_id=:id AND status='queued'",
            {"id": candidate["job_id"], "at": started},
        )
        db.commit()
        if claimed:
            return get_job(db, str(candidate["job_id"]))
    return None


def work_once(settings: ApiSettings) -> bool:
    """Claim at most one job, recover orphans, then run on the lock-owning SQL session."""
    with connect(settings.config.sql, settings.target_database) as db:
        # An idle poll must not compete with submissions, reports or weekly execution.
        # Recheck and claim under the locks below; a new job after this read waits one poll.
        if not _has_work(db):
            return False
        try:
            with execution_lock(db, settings):
                for orphan in db.query("SELECT * FROM run_jobs WHERE status='running'"):
                    # A result this attempt committed is complete: its CSVs render from SQL.
                    completed = committed_result(db, orphan)
                    finish_job(
                        db,
                        str(orphan["job_id"]),
                        "completed" if completed else "failed",
                        None
                        if completed
                        else "Worker interrupted. Retry this week."
                        if orphan.get("backfill_id")
                        else "Worker interrupted. Start a new run to retry.",
                    )
                started = datetime.now(UTC).replace(tzinfo=None)
                started = started.replace(microsecond=started.microsecond // 1000 * 1000)
                job = db.query_one(
                    "SELECT TOP 1 * FROM run_jobs WHERE status='pending' ORDER BY submitted_at"
                )
                if job is not None:
                    db.execute(
                        "UPDATE run_jobs SET status='running',started_at=:at WHERE job_id=:id",
                        {"id": job["job_id"], "at": started},
                    )
                    db.commit()
                else:
                    job = claim_queued(db, started)
                    if job is None:
                        return False
                job["started_at"] = started
                log.info(
                    "run.job_started",
                    job_id=job["job_id"],
                    team_id=job["team_id"],
                    run_id=job["run_id"],
                    backfill_id=job.get("backfill_id"),
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
                        None
                        if completed
                        else "Analysis failed. Check the service logs, then retry this week."
                        if job.get("backfill_id")
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
