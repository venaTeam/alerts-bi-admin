"""SQL-backed submissions and one manual-run worker, independent of HTTP requests.

The session lock lives on the execution connection, surviving journal/persistence commits.
A new worker can recover running jobs only after the previous session released its lock.
Queued jobs (backfill weeks, and manual runs accepted while another run is in progress or an
older manual run is queued) hold no slot; the worker claims them after pending work
(docs/backfill.md). With WORKER_CONCURRENCY above 1 it runs up to that many jobs at once, one
per team, each on a connection of its own (design "Parallel runs across teams").
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager, nullcontext, suppress
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from alerts_bi_operations.registry import RegistryError, load_registry, select_team
from alerts_bi_shared.db.connection import Database, connect
from alerts_bi_shared.hashing import sha256_text
from alerts_bi_shared.logging_setup import log, log_context
from alerts_bi_shared.ui.explain import format_week
from alerts_bi_shared.versions import APP_VERSION
from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError

from ..config import ApiSettings
from ..db.llm_audit import ReviewSettingsChanged
from ..run.orchestrator import compute_run_id
from ..weekly.runner import (
    BACKFILL_YIELD_RESOURCE,
    WeeklyBusy,
    schedule_lock,
)
from .schemas import RunRequest
from .service import close_client, execute_request, latest_week_end, select_client

LOCK_RESOURCE = "alerts-bi-manual-run"
ACTIVE_STATES = ("queued", "pending", "running")
_ACTIVE_SQL = "(" + ", ".join(f"'{state}'" for state in ACTIVE_STATES) + ")"
QUEUE_LOCK_PREFIX = "alerts-bi-queue:"
PUBLISHED_WHILE_WAITING = (
    "Not run: this week was published while the run waited. Withdraw it to run it again."
)
#: Most jobs one worker runs at once. Each adds an Elasticsearch scan, model calls and a
#: week of alerts in memory, so a typo such as 30 is refused rather than obeyed.
MAX_CONCURRENCY = 8
#: Numbered slots and one active job per team; without it the database allows one slot.
PARALLEL_MIGRATION = "021_parallel_run_slots"


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
    week or run accepted after its own checks (backfill admission, and manual admission while
    the execution lock is busy, bypass the execution lock).
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
    if db.query_one("SELECT TOP 1 job_id FROM run_jobs WHERE active_slot IS NOT NULL"):
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
    # A second job for the same week would run it again and replace the first result.
    active = db.query_one(
        "SELECT TOP 1 status FROM run_jobs WHERE team_id=:team AND run_at=:at "
        f"AND status IN {_ACTIVE_SQL}",
        {"team": body.team, "at": run_at.replace(tzinfo=None)},
    )
    if active:
        week = format_week(window.window_start, window.run_at)
        advice = (
            "Wait for it to finish, or remove it from the queue."
            if active["status"] == "queued"
            else "Wait for it to finish."
        )
        raise HTTPException(409, f"Already queued or running for this team: {week}. {advice}")
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
        "Accepted a manual run for the week ending {week}; {placement}",
        week=f"{run_at:%Y-%m-%d}",
        placement="next for the worker"
        if status == "pending"
        else "queued behind the runs ahead of it, before any remaining backfill weeks",
        llm=body.llm,
        status=status,
        job_id=job_id,
        team_id=body.team,
        run_id=run_id,
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
                # Wait behind the active job and behind manual runs already queued, so a
                # submission between the worker's polls cannot start before an older one.
                status = "queued" if _active_or_queued_manual(db) else "pending"
                return _admit(settings, db, body, idempotency_key, key_hash, request_hash, status)
        except ExecutionBusy:
            db.rollback()
        except HTTPException as exc:
            db.rollback()
            # The recheck above reads committed rows only, so an identical submission queued
            # without the lock may commit while this one waits for the team's queue lock and
            # then count as a duplicate week. A retry with the same key gets that row.
            if exc.status_code == 409:
                existing = _existing_submission(db, key_hash, request_hash)
                if existing:
                    return existing
            raise
        # A job, CLI run, report, weekly run or deletion holds the execution lock. Queue this
        # run behind it; a queued row holds no slot, so admission needs no execution lock.
        try:
            return _admit(settings, db, body, idempotency_key, key_hash, request_hash, "queued")
        except Exception:
            db.rollback()
            # A simultaneous identical submission wins the unique key; return its row.
            existing = _existing_submission(db, key_hash, request_hash)
            if existing:
                return existing
            raise


def cancel_queued_job(db: Database, job: Mapping[str, Any]) -> bool:
    """Remove one queued manual run; a run the worker already claimed runs to completion."""
    # The worker claims only a still-queued row, so this update and a claim cannot both win.
    cancelled = db.query(
        "UPDATE run_jobs SET status='cancelled',completed_at=:now,error_summary=:message "
        "OUTPUT inserted.job_id WHERE job_id=:id AND status='queued' AND backfill_id IS NULL",
        {
            "id": job["job_id"],
            "now": datetime.now(UTC).replace(tzinfo=None),
            "message": "Cancelled before it started.",
        },
    )
    db.commit()
    if cancelled:
        log.info(
            "run.job_cancelled",
            "Removed the queued manual run for the week ending {week}",
            week=f"{job['run_at']:%Y-%m-%d}",
            job_id=job["job_id"],
            team_id=job["team_id"],
            run_id=job["run_id"],
        )
    return bool(cancelled)


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


def _active_or_queued_manual(db: Database) -> bool:
    """Does a job hold an active slot, or a manual run wait in the queue?"""
    return (
        db.query_one(
            "SELECT TOP 1 job_id FROM run_jobs WHERE active_slot IS NOT NULL "
            "OR (status='queued' AND backfill_id IS NULL)"
        )
        is not None
    )


def _has_work(db: Database) -> bool:
    if _active_or_queued_manual(db):
        return True
    queued = db.query_one(
        "SELECT TOP 1 job_id FROM run_jobs WHERE status='queued' AND backfill_id IS NOT NULL"
    )
    return queued is not None and _may_claim_backfill(db)


def claim_queued(db: Database, started: datetime, slot: int = 1) -> dict[str, Any] | None:
    """Claim the next queued job: manual runs first, then backfill weeks oldest first.

    A team runs one job at a time, so a queued job waits while its team holds a slot.
    """
    candidates = db.query(
        "SELECT TOP 5 j.job_id FROM run_jobs j "
        "LEFT JOIN run_backfills b ON b.backfill_id=j.backfill_id "
        "WHERE j.status='queued' AND (j.backfill_id IS NULL OR :backfill=1) "
        "AND NOT EXISTS (SELECT 1 FROM run_jobs a WHERE a.team_id=j.team_id "
        "AND a.active_slot IS NOT NULL) "
        # Manual runs keep submission order; backfill weeks go by backfill, then oldest week.
        "ORDER BY CASE WHEN j.backfill_id IS NULL THEN 0 ELSE 1 END, "
        "CASE WHEN j.backfill_id IS NULL THEN j.submitted_at END, b.submitted_at, "
        "j.run_at, j.submitted_at, j.job_id",
        {"backfill": 1 if _may_claim_backfill(db) else 0},
    )
    for candidate in candidates:
        # A cancellation may have committed since the read; claim only a still-queued row.
        claimed = db.query(
            "UPDATE run_jobs SET status='running',active_slot=:slot,started_at=:at "
            "OUTPUT inserted.job_id WHERE job_id=:id AND status='queued'",
            {"id": candidate["job_id"], "at": started, "slot": slot},
        )
        db.commit()
        if claimed:
            return get_job(db, str(candidate["job_id"]))
    return None


def _recover_orphans(db: Database) -> None:
    """Finish jobs left running by an interrupted worker; call only under the execution lock."""
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
        log.warn(
            "run.job_recovered",
            "Found a job still running from an interrupted worker (started "
            "{started:utc}); {outcome}",
            started=orphan["started_at"],
            outcome="its result was stored, so it is marked completed"
            if completed
            else "no result was stored, so it is marked failed",
            team_id=orphan["team_id"],
            run_id=orphan["run_id"],
            job_id=orphan["job_id"],
            backfill_id=orphan.get("backfill_id"),
        )


def _claim_next(db: Database, slot: int) -> dict[str, Any] | None:
    """The pending job (which already holds slot 1), else the next queued job in ``slot``."""
    started = datetime.now(UTC).replace(tzinfo=None)
    started = started.replace(microsecond=started.microsecond // 1000 * 1000)
    job = db.query_one("SELECT TOP 1 * FROM run_jobs WHERE status='pending' ORDER BY submitted_at")
    if job is not None:
        db.execute(
            "UPDATE run_jobs SET status='running',started_at=:at WHERE job_id=:id",
            {"id": job["job_id"], "at": started},
        )
        db.commit()
    else:
        job = claim_queued(db, started, slot)
        if job is None:
            return None
    job["started_at"] = started
    return job


@contextmanager
def _job_context(job: Mapping[str, Any]) -> Iterator[None]:
    with log_context(
        team_id=job["team_id"],
        run_id=job["run_id"],
        job_id=job["job_id"],
        backfill_id=job.get("backfill_id"),
    ):
        yield


def work_once(settings: ApiSettings) -> bool:
    """Claim at most one job, recover orphans, then run on the lock-owning SQL session."""
    with connect(settings.config.sql, settings.target_database) as db:
        # An idle poll must not compete with submissions, reports or weekly execution.
        # Recheck and claim under the locks below; a new job after this read waits one poll.
        if not _has_work(db):
            return False
        try:
            with execution_lock(db, settings):
                _recover_orphans(db)
                job = _claim_next(db, 1)
                if job is None:
                    return False
                with _job_context(job):
                    _run_job(settings, db, job)
                return True
        except HTTPException as exc:
            if exc.status_code == 409:
                return False
            raise


def work_parallel(
    settings: ApiSettings, slots: int, *, poll_seconds: float = 2, refill: bool = True
) -> bool:
    """Run up to ``slots`` jobs at once, one per team, until none is left to claim.

    This session holds the execution locks while any job runs, so CLI runs, reports, the
    weekly schedule and synchronous runs still find the worker busy, and recovery still
    happens only before the first claim. Each job runs on a connection of its own. A job that
    ends in an infrastructure error stops further claims; the others finish, then the error
    exits the worker for its supervisor to restart. Without ``refill`` (``--once``) only the
    first claims run and freed slots stay empty. Returns whether any job ran.
    """
    with connect(settings.config.sql, settings.target_database) as db:
        if not _has_work(db):
            return False
        try:
            with execution_lock(db, settings):
                _recover_orphans(db)
                return _run_slots(settings, db, slots, poll_seconds, refill=refill)
        except HTTPException as exc:
            if exc.status_code == 409:
                return False
            raise


def _run_slots(
    settings: ApiSettings, db: Database, slots: int, poll_seconds: float, *, refill: bool = True
) -> bool:
    # A deletion beside running jobs locks their rows and can deadlock with a claim. Let the
    # claim lose: the operator's deletion goes through and the claim is retried at the next poll.
    db.execute("SET DEADLOCK_PRIORITY LOW")
    running: dict[Future[None], tuple[int, dict[str, Any]]] = {}
    failure: BaseException | None = None
    failed_claims = 0
    claiming = True
    worked = False
    with ThreadPoolExecutor(max_workers=slots, thread_name_prefix="run-job") as pool:
        while True:
            taken = {slot for slot, _job in running.values()}
            free = [slot for slot in range(1, slots + 1) if slot not in taken]
            while claiming and failure is None and free:
                try:
                    job = _claim_next(db, free[0])
                except DBAPIError as exc:
                    failed_claims += 1
                    failure = _claim_failed(db, exc, failed_claims, len(running), poll_seconds)
                    break
                failed_claims = 0
                if job is None:
                    break
                slot = int(job["active_slot"])
                running[pool.submit(_run_on_own_connection, settings, job)] = (slot, job)
                free.remove(slot)
                worked = True
            claiming = refill
            if not running:
                break
            # Wake on a finished job, or after a poll to start work submitted meanwhile.
            done, _ = wait(running, timeout=poll_seconds, return_when=FIRST_COMPLETED)
            for future in done:
                slot, job = running.pop(future)
                error = future.exception()
                if error is None:
                    continue
                with _job_context(job):
                    if failure is None:
                        failure = error
                        log.error(
                            "worker.stopping",
                            "The job in slot {slot} stopped on an error the worker cannot "
                            "recover from; starting no more jobs and exiting when the "
                            "{running} still running finish",
                            slot=slot,
                            running=len(running),
                            exc=error,
                        )
                    else:
                        # Each lost job is recovered as interrupted; name every one of them.
                        log.error(
                            "worker.job_lost",
                            "The job in slot {slot} also stopped on an error the worker "
                            "cannot recover from while the worker was stopping",
                            slot=slot,
                            exc=error,
                        )
    if failure is not None:
        raise failure
    return worked


#: Claims that fail in a row before the worker stops; a deadlock with a deletion fails one.
CLAIM_ATTEMPTS = 3


def _claim_failed(
    db: Database, exc: DBAPIError, attempts: int, running: int, poll_seconds: float
) -> DBAPIError | None:
    """Log a failed claim; return it as the worker's failure once it has failed too often."""
    # A rollback fails only when the connection itself is gone; the next claim fails the same way.
    with suppress(Exception):
        db.rollback()
    if attempts < CLAIM_ATTEMPTS:
        log.warn(
            "worker.claim_failed",
            "Claiming the next job failed; trying again in {poll:dur}",
            poll=poll_seconds,
            attempts=attempts,
            exc=exc,
        )
        return None
    log.error(
        "worker.stopping",
        "Claiming the next job failed {attempts} times in a row; starting no more jobs and "
        "exiting when the {running} still running finish",
        attempts=attempts,
        running=running,
        exc=exc,
    )
    return exc


def _run_on_own_connection(settings: ApiSettings, job: dict[str, Any]) -> None:
    with connect(settings.config.sql, settings.target_database) as db, _job_context(job):
        _run_job(settings, db, job)


def _run_job(settings: ApiSettings, db: Database, job: dict[str, Any]) -> None:
    """Execute one claimed job on the lock-owning session and record how it ended."""
    kind = "backfill week" if job.get("backfill_id") else "manual run"
    submitted = job.get("submitted_at")
    queued = (
        (job["started_at"] - submitted).total_seconds() if isinstance(submitted, datetime) else 0
    )
    if db.query_one(
        "SELECT run_id FROM review_publications WHERE run_id=:id AND withdrawn_at IS NULL",
        {"id": job["run_id"]},
    ):
        # Published while it waited (by the weekly schedule, say). Persistence would refuse it
        # only after a full Elasticsearch read, holding the one slot every team waits for.
        finish_job(db, str(job["job_id"]), "failed", PUBLISHED_WHILE_WAITING)
        log.warn(
            "run.job_skipped",
            "Skipped the {kind} for the week ending {week}: the week was published while it "
            "waited {queued:dur} in the queue",
            kind=kind,
            week=f"{job['run_at']:%Y-%m-%d}",
            queued=queued,
        )
        return
    log.info(
        "run.job_started",
        "Started the {kind} for the week ending {week} after {queued:dur} in the queue",
        kind=kind,
        week=f"{job['run_at']:%Y-%m-%d}",
        queued=queued,
        llm=job["llm_mode"],
        slot=job.get("active_slot"),
    )
    clock = time.monotonic()
    try:
        registry = (
            load_registry(settings.registry_path) if settings.registry_path else load_registry()
        )
        if registry.file_sha256 != job["registry_sha256"]:
            raise ValueError("Registry changed since submission. Start a new run.")
        body = RunRequest(team=job["team_id"], run_at=job["run_at"], llm=job["llm_mode"])
        execute_request(settings, db, body, expected_run_id=str(job["run_id"]))
        finish_job(db, str(job["job_id"]), "completed")
    except Exception as exc:
        db.rollback()
        completed = committed_result(db, job)
        if isinstance(exc, ReviewSettingsChanged):
            advice = (
                "The model settings changed since this week was first sent to the model. "
                "Delete this run, then run the week again."
            )
        elif job.get("backfill_id"):
            advice = "Analysis failed. Check the service logs, then retry this week."
        else:
            advice = "Analysis failed. Check the service logs, then start a new run to retry."
        elapsed = time.monotonic() - clock
        # Record the failure before updating the job, so it survives a failed update.
        if completed:
            log.warn(
                "run.job_failed",
                "The {kind} raised an error after its result was stored; marking it completed",
                kind=kind,
                elapsed=elapsed,
                exc=exc,
            )
        else:
            log.error(
                "run.job_failed",
                "The {kind} failed after {elapsed:dur}",
                kind=kind,
                elapsed=elapsed,
                exc=exc,
            )
        finish_job(
            db,
            str(job["job_id"]),
            "completed" if completed else "failed",
            None if completed else advice,
        )
        if completed:
            log.info(
                "run.job_finished",
                "Job completed: queued {queued:dur}, ran {elapsed:dur}",
                queued=queued,
                elapsed=elapsed,
                status="completed",
            )
            return
        log.error(
            "run.job_finished",
            "Job failed: queued {queued:dur}, ran {elapsed:dur}. The console tells the "
            'operator: "{advice}"',
            queued=queued,
            elapsed=elapsed,
            advice=advice,
            status="failed",
        )
        return
    log.info(
        "run.job_finished",
        "Job completed: queued {queued:dur}, ran {elapsed:dur}",
        queued=queued,
        elapsed=time.monotonic() - clock,
        status="completed",
    )


#: An idle worker says so this often, so a quiet log is not mistaken for a stuck worker.
IDLE_HEARTBEAT_SECONDS = 15 * 60


def run_worker(
    settings: ApiSettings,
    *,
    once: bool = False,
    concurrency: int = 1,
    poll_seconds: float = 2,
    heartbeat_seconds: float = IDLE_HEARTBEAT_SECONDS,
) -> None:
    """The supervisor restarts a worker after infrastructure errors; jobs stay durable.

    With ``concurrency`` 1 each pass runs one job on the lock-owning session, as before
    parallel slots existed; above 1 a pass runs jobs side by side until none is left. With
    ``once`` above 1, the pass runs only the jobs it claims first, up to one per slot.
    """
    check_concurrency(settings, concurrency)
    _log_worker_started(settings, poll_seconds, concurrency)
    idle_since = last_heartbeat = time.monotonic()
    while True:
        worked = (
            work_once(settings)
            if concurrency == 1
            else work_parallel(settings, concurrency, poll_seconds=poll_seconds, refill=not once)
        )
        if once:
            return
        now = time.monotonic()
        if worked:
            idle_since = last_heartbeat = now
            continue
        if now - last_heartbeat >= heartbeat_seconds:
            _log_idle(settings, now - idle_since)
            last_heartbeat = now
        time.sleep(poll_seconds)


def check_concurrency(settings: ApiSettings, concurrency: int) -> None:
    """Refuse a WORKER_CONCURRENCY outside 1..8, or above 1 before migration 021."""
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(
            f"WORKER_CONCURRENCY must be between 1 and {MAX_CONCURRENCY}, got {concurrency}"
        )
    if concurrency == 1:
        return
    with connect(settings.config.sql, settings.target_database) as db:
        applied = db.query_one(
            "SELECT 1 AS ready FROM schema_migrations WHERE version=:version",
            {"version": PARALLEL_MIGRATION},
        )
    if applied is None:
        raise ValueError(
            f"WORKER_CONCURRENCY={concurrency} needs database migration {PARALLEL_MIGRATION}; "
            "run `alerts-bi-admin db setup`, or set WORKER_CONCURRENCY=1"
        )


def _log_worker_started(settings: ApiSettings, poll_seconds: float, concurrency: int) -> None:
    config = settings.config
    try:
        registry = (
            load_registry(settings.registry_path) if settings.registry_path else load_registry()
        )
        registry_version: str | None = registry.registry_version
    except Exception as exc:
        # Each job loads the registry itself and fails with the reason; say so up front.
        log.warn("worker.registry_unreadable", "The team registry cannot be read", exc=exc)
        registry_version = None
    log.info(
        "worker.started",
        "Run worker {version} ready, polling {database} every {poll:dur}; {jobs}",
        version=APP_VERSION,
        database=settings.target_database,
        poll=poll_seconds,
        jobs="one job at a time"
        if concurrency == 1
        else f"up to {concurrency} jobs at once, one per team",
        concurrency=concurrency,
        sql=f"{config.sql.host}:{config.sql.port}",
        es=_host_and_port(config.es.url),
        indexes=[config.es.v1_index, config.es.v2_index],
        llm=config.llm.model if config.llm.enabled else "disabled",
        registry=settings.registry_path or "packaged",
        registry_version=registry_version,
    )


def _host_and_port(url: str) -> str:
    """The host of a service URL without credentials; a malformed URL is the jobs' to report."""
    try:
        parts = urlsplit(url)
        return f"{parts.hostname or ''}{f':{parts.port}' if parts.port else ''}"
    except ValueError:
        return "(invalid ES_URL)"


def _log_idle(settings: ApiSettings, idle: float) -> None:
    try:
        with connect(settings.config.sql, settings.target_database) as db:
            counts = {
                str(row["status"]): int(row["n"])
                for row in db.query(
                    "SELECT status, COUNT(*) AS n FROM run_jobs "
                    "WHERE status IN ('pending','queued','running') GROUP BY status"
                )
            }
            backfill_blocked = not _may_claim_backfill(db)
    except Exception as exc:
        log.warn(
            "worker.idle", "Idle for {idle:dur}; the job queue cannot be read", idle=idle, exc=exc
        )
        return
    waiting = sum(counts.values())
    if not waiting:
        reason = "nothing is pending or queued"
    elif backfill_blocked and counts.get("queued"):
        reason = "backfill weeks wait while the weekly job runs"
    else:
        reason = "jobs wait for the execution lock (a CLI run, report, weekly job or deletion)"
    log.info(
        "worker.idle",
        "Idle for {idle:dur}; {reason}",
        idle=idle,
        reason=reason,
        pending=counts.get("pending", 0),
        queued=counts.get("queued", 0),
        running=counts.get("running", 0),
    )
