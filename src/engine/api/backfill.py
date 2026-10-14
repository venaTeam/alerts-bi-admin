"""Operator multi-week backfill: admission, cancellation, retry and reads (docs/backfill.md).

A backfill is one accepted request for one team. Each week is an ordinary ``run_jobs`` row
that starts ``queued`` and holds no active slot; :mod:`.jobs` claims queued weeks one at a
time, oldest first, after manual work. Admission performs no Elasticsearch or model call and
takes no execution lock, so it is accepted while another week is running.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from alerts_bi_operations.registry import (
    LoadedRegistry,
    RegistryError,
    load_registry,
    select_team,
)
from alerts_bi_shared.db.connection import Database, connect
from alerts_bi_shared.hashing import sha256_text
from alerts_bi_shared.logging_setup import log
from alerts_bi_shared.ui.explain import format_week
from alerts_bi_shared.window import build_run_window
from fastapi import HTTPException

from ..config import ApiSettings
from ..run.orchestrator import compute_run_id
from ..weekly.plan import RETENTION_DAYS, is_week_boundary, latest_week_end
from .jobs import ACTIVE_STATES, get_job
from .schemas import LlmMode, RunRequest
from .service import close_client, select_client

__all__ = [
    "MAX_WEEKS",
    "backfill_jobs",
    "cancel_backfill",
    "dismiss_backfill",
    "get_backfill",
    "latest_backfill",
    "parse_weeks",
    "retry_backfill",
    "selectable_weeks",
    "submit_backfill",
    "week_rows",
]

#: A retained range never holds more than twelve completed Monday weeks.
MAX_WEEKS = 12
WEEK = timedelta(days=7)
_ACTIVE_SQL = "('queued', 'pending', 'running')"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _naive(value: datetime) -> datetime:
    return _utc(value).replace(tzinfo=None)


def label(end: datetime) -> str:
    return format_week(end - WEEK, end)


def selectable_weeks(now: datetime) -> tuple[datetime, datetime]:
    """The earliest and latest Monday window ends a backfill can pick at ``now``.

    A week is retained while its window start is no older than :data:`RETENTION_DAYS`,
    the same boundary the weekly schedule uses to call a week expired.
    """
    latest = latest_week_end(now)
    cutoff = _utc(now) - timedelta(days=RETENTION_DAYS)
    earliest = latest
    while earliest - 2 * WEEK >= cutoff:
        earliest -= WEEK
    return earliest, latest


def parse_weeks(values: Sequence[str]) -> list[datetime]:
    """Distinct Monday window ends from ``YYYY-MM-DD`` form values, oldest first."""
    weeks: set[datetime] = set()
    for value in values:
        try:
            end = datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            raise HTTPException(400, "Choose weeks by the Monday each one ends on.") from None
        if not is_week_boundary(end):
            raise HTTPException(400, f"{value} is not a Monday. Choose the Monday a week ends on.")
        weeks.add(end)
    return sorted(weeks)


def _validate_operator(operator: str, idempotency_key: str) -> None:
    if not 1 <= len(idempotency_key) <= 64 or not idempotency_key.isascii():
        raise HTTPException(400, "The form has expired. Reload the page and try again.")
    if not operator or len(operator) > 256:
        raise HTTPException(400, "The operator identity is invalid.")


def _check_retained(weeks: Sequence[datetime], now: datetime) -> None:
    earliest, latest = selectable_weeks(now)
    for end in weeks:
        if end > latest:
            raise HTTPException(400, f"{label(end)} has not finished yet. Choose completed weeks.")
        if end < earliest:
            raise HTTPException(
                400,
                f"{label(end)} is past the {RETENTION_DAYS}-day Elasticsearch retention "
                "and can no longer be backfilled.",
            )


def _registry(settings: ApiSettings, team_id: str) -> LoadedRegistry:
    try:
        registry = (
            load_registry(settings.registry_path) if settings.registry_path else load_registry()
        )
        select_team(registry, team_id)
    except RegistryError as exc:
        raise HTTPException(400, str(exc)) from None
    return registry


def _run_ids(
    settings: ApiSettings,
    registry: LoadedRegistry,
    team_id: str,
    weeks: Sequence[datetime],
    llm: LlmMode,
) -> dict[datetime, str]:
    client, _ = select_client(settings.config, llm)
    model_version = client.model_version if client else None
    close_client(client)
    result = {}
    for end in weeks:
        window = build_run_window(end)
        result[end] = compute_run_id(
            team_id=team_id,
            run_at=window.run_at,
            window_start=window.window_start,
            registry_sha256=registry.file_sha256,
            model_version=model_version,
        )
    return result


def _in_list(weeks: Sequence[datetime]) -> tuple[str, dict[str, Any]]:
    names = {f"w{index}": _naive(end) for index, end in enumerate(weeks)}
    return ", ".join(f":{name}" for name in names), names


def _team_lock(db: Database, team_id: str) -> None:
    """Serialize backfill admission and retry for one team inside the current transaction."""
    row = db.query_one(
        "SET NOCOUNT ON; DECLARE @result INT; "
        "EXEC @result = sp_getapplock @Resource=:resource, @LockMode='Exclusive', "
        "@LockOwner='Transaction', @LockTimeout=10000; SELECT @result AS result",
        {"resource": f"alerts-bi-backfill:{team_id}"[:255]},
    )
    if row is None or int(row["result"]) < 0:
        raise HTTPException(
            409, "Another backfill request for this team is being saved. Try again."
        )


def _active_backfill(db: Database, team_id: str, other_than: str | None = None) -> str | None:
    row = db.query_one(
        "SELECT TOP 1 b.backfill_id FROM run_backfills b WHERE b.team_id=:team "
        "AND (:other IS NULL OR b.backfill_id<>:other) AND EXISTS (SELECT 1 FROM run_jobs j "
        f"WHERE j.backfill_id=b.backfill_id AND j.status IN {_ACTIVE_SQL})",
        {"team": team_id, "other": other_than},
    )
    return str(row["backfill_id"]) if row else None


def _refuse_locked_weeks(db: Database, team_id: str, weeks: Sequence[datetime]) -> None:
    placeholders, params = _in_list(weeks)
    published = db.query(
        "SELECT DISTINCT window_end FROM review_publications WHERE team_id=:team "
        f"AND withdrawn_at IS NULL AND window_end IN ({placeholders}) ORDER BY window_end",
        {"team": team_id, **params},
    )
    if published:
        names = ", ".join(label(_utc(row["window_end"])) for row in published)
        raise HTTPException(409, f"Published weeks are locked: {names}. Withdraw them first.")
    active = db.query(
        "SELECT DISTINCT run_at FROM run_jobs WHERE team_id=:team "
        f"AND status IN {_ACTIVE_SQL} AND run_at IN ({placeholders}) ORDER BY run_at",
        {"team": team_id, **params},
    )
    if active:
        names = ", ".join(label(_utc(row["run_at"])) for row in active)
        raise HTTPException(409, f"Already queued or running: {names}.")


def _insert_week(
    db: Database,
    *,
    backfill_id: str,
    team_id: str,
    end: datetime,
    llm: LlmMode,
    operator: str,
    registry_sha256: str,
    run_id: str,
    attempt: int,
    now: datetime,
) -> str:
    job_id = uuid4().hex
    key = f"bf-{backfill_id}-{end:%Y%m%d}-{attempt}"
    db.execute(
        "INSERT INTO run_jobs (job_id,idempotency_key,idempotency_key_hash,requested_by,"
        "request_hash,team_id,run_at,llm_mode,registry_sha256,run_id,status,active_slot,"
        "submitted_at,backfill_id) VALUES (:id,:key,:key_hash,:operator,:hash,:team,:at,:mode,"
        ":registry,:run,'queued',NULL,:now,:backfill)",
        {
            "id": job_id,
            "key": key,
            "key_hash": bytes.fromhex(sha256_text(key)),
            "operator": operator,
            "hash": sha256_text(RunRequest(team=team_id, run_at=end, llm=llm).model_dump_json()),
            "team": team_id,
            "at": _naive(end),
            "mode": llm,
            "registry": registry_sha256,
            "run": run_id,
            "now": now,
            "backfill": backfill_id,
        },
    )
    return job_id


def _now() -> datetime:
    now = datetime.now(UTC).replace(tzinfo=None)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def submit_backfill(
    settings: ApiSettings,
    team_id: str,
    weeks: Sequence[datetime],
    llm: LlmMode,
    operator: str,
    idempotency_key: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Accept one queued run per week, or refuse the whole request and store nothing."""
    _validate_operator(operator, idempotency_key)
    weeks = sorted({_utc(end) for end in weeks})
    if not weeks:
        raise HTTPException(400, "Choose at least one week to backfill.")
    if len(weeks) > MAX_WEEKS:
        raise HTTPException(400, f"Choose at most {MAX_WEEKS} weeks.")
    _check_retained(weeks, _utc(now or datetime.now(UTC)))
    request_hash = sha256_text(
        json.dumps(
            {"team": team_id, "weeks": [end.date().isoformat() for end in weeks], "llm": llm}
        )
    )
    key_hash = bytes.fromhex(sha256_text(idempotency_key))
    registry = _registry(settings, team_id)
    run_ids = _run_ids(settings, registry, team_id, weeks, llm)
    with connect(settings.config.sql, settings.target_database) as db, db.transaction():
        _team_lock(db, team_id)
        existing = db.query_one(
            "SELECT * FROM run_backfills WHERE requested_by=:operator "
            "AND idempotency_key_hash=:key_hash",
            {"operator": operator, "key_hash": key_hash},
        )
        if existing:
            if existing["request_hash"] != request_hash:
                raise HTTPException(
                    409, "This form was already submitted with different weeks or options."
                )
            return existing
        if _active_backfill(db, team_id):
            raise HTTPException(
                409,
                "A backfill is already in progress for this team. Wait for it to finish "
                "or cancel its queued weeks.",
            )
        _refuse_locked_weeks(db, team_id, weeks)
        backfill_id = uuid4().hex
        submitted = _now()
        db.execute(
            "INSERT INTO run_backfills (backfill_id,team_id,requested_by,idempotency_key_hash,"
            "request_hash,llm_mode,week_count,submitted_at) VALUES (:id,:team,:operator,"
            ":key_hash,:hash,:mode,:count,:now)",
            {
                "id": backfill_id,
                "team": team_id,
                "operator": operator,
                "key_hash": key_hash,
                "hash": request_hash,
                "mode": llm,
                "count": len(weeks),
                "now": submitted,
            },
        )
        for end in weeks:
            _insert_week(
                db,
                backfill_id=backfill_id,
                team_id=team_id,
                end=end,
                llm=llm,
                operator=operator,
                registry_sha256=registry.file_sha256,
                run_id=run_ids[end],
                attempt=1,
                now=submitted,
            )
        row = get_backfill(db, backfill_id)
    log.info(
        "run.backfill_submitted",
        backfill_id=backfill_id,
        team_id=team_id,
        weeks=len(weeks),
        llm_mode=llm,
        operator=operator,
    )
    return row


def get_backfill(db: Database, backfill_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM run_backfills WHERE backfill_id=:id", {"id": backfill_id})
    if row is None:
        raise HTTPException(404, "No such backfill.")
    return row


def latest_backfill(db: Database, team_id: str) -> dict[str, Any] | None:
    """The team's newest backfill that is still shown: active, or finished and not dismissed."""
    return db.query_one(
        "SELECT TOP 1 * FROM run_backfills WHERE team_id=:team AND dismissed_at IS NULL "
        "ORDER BY submitted_at DESC, backfill_id DESC",
        {"team": team_id},
    )


def backfill_jobs(db: Database, backfill_id: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT * FROM run_jobs WHERE backfill_id=:id ORDER BY run_at, submitted_at, job_id",
        {"id": backfill_id},
    )


def week_rows(jobs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The latest job for each week, oldest week first, with its attempt count."""
    latest: dict[datetime, dict[str, Any]] = {}
    attempts: dict[datetime, int] = {}
    for job in jobs:
        attempts[job["run_at"]] = attempts.get(job["run_at"], 0) + 1
        latest[job["run_at"]] = job
    return [latest[at] | {"attempts": attempts[at]} for at in sorted(latest)]


def cancel_backfill(
    db: Database, backfill_id: str, operator: str, job_id: str | None = None
) -> tuple[dict[str, Any], int]:
    """Cancel every queued week, or one; a claimed week runs to completion."""
    backfill = get_backfill(db, backfill_id)
    params: dict[str, Any] = {
        "id": backfill_id,
        "job": job_id,
        "now": _now(),
        "message": f"Cancelled by {operator} before it started."[:1000],
    }
    cancelled = db.query(
        "UPDATE run_jobs SET status='cancelled',completed_at=:now,error_summary=:message "
        "OUTPUT inserted.job_id WHERE backfill_id=:id AND status='queued' "
        "AND (:job IS NULL OR job_id=:job)",
        params,
    )
    if job_id is None:
        db.execute(
            "UPDATE run_backfills SET cancelled_at=:now,cancelled_by=:operator "
            "WHERE backfill_id=:id AND cancelled_at IS NULL",
            {"id": backfill_id, "now": params["now"], "operator": operator},
        )
    db.commit()
    log.info(
        "run.backfill_cancelled",
        backfill_id=backfill_id,
        job_id=job_id,
        cancelled=len(cancelled),
        operator=operator,
    )
    return backfill, len(cancelled)


def retry_backfill(
    settings: ApiSettings,
    backfill_id: str,
    operator: str,
    job_id: str | None = None,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], int]:
    """Queue a new attempt for each failed week (or one), refusing the whole request."""
    with connect(settings.config.sql, settings.target_database) as db:
        backfill = get_backfill(db, backfill_id)
        team_id = str(backfill["team_id"])
        failed = [
            row for row in week_rows(backfill_jobs(db, backfill_id)) if row["status"] == "failed"
        ]
        if job_id is not None:
            failed = [row for row in failed if row["job_id"] == job_id]
        if not failed:
            raise HTTPException(409, "Only the latest failed attempt of a week can be retried.")
        weeks = [_utc(row["run_at"]) for row in failed]
        _check_retained(weeks, _utc(now or datetime.now(UTC)))
        llm: LlmMode = backfill["llm_mode"]
        registry = _registry(settings, team_id)
        run_ids = _run_ids(settings, registry, team_id, weeks, llm)
        with db.transaction():
            _team_lock(db, team_id)
            if _active_backfill(db, team_id, other_than=backfill_id):
                raise HTTPException(
                    409, "Another backfill is in progress for this team. Retry after it finishes."
                )
            _refuse_locked_weeks(db, team_id, weeks)
            submitted = _now()
            for row, end in zip(failed, weeks, strict=True):
                _insert_week(
                    db,
                    backfill_id=backfill_id,
                    team_id=team_id,
                    end=end,
                    llm=llm,
                    operator=operator,
                    registry_sha256=registry.file_sha256,
                    run_id=run_ids[end],
                    attempt=int(row["attempts"]) + 1,
                    now=submitted,
                )
    log.info(
        "run.backfill_retried",
        backfill_id=backfill_id,
        weeks=len(weeks),
        operator=operator,
    )
    return backfill, len(weeks)


def dismiss_backfill(db: Database, backfill_id: str) -> dict[str, Any]:
    backfill = get_backfill(db, backfill_id)
    if any(job["status"] in ACTIVE_STATES for job in backfill_jobs(db, backfill_id)):
        raise HTTPException(409, "This backfill still has weeks to run. Cancel them first.")
    db.execute(
        "UPDATE run_backfills SET dismissed_at=:now WHERE backfill_id=:id AND dismissed_at IS NULL",
        {"id": backfill_id, "now": _now()},
    )
    db.commit()
    return backfill


def job_backfill(db: Database, job_id: str) -> str:
    """The backfill a job belongs to, refusing manual submissions."""
    job = get_job(db, job_id)
    if not job.get("backfill_id"):
        raise HTTPException(409, "Only backfill weeks can be removed or retried here.")
    return str(job["backfill_id"])
