"""Permanent deletion of a run and its own records, never shared caches.

A run that failed before storing results has no ``runs`` row, but its submissions, analysis
checkpoint and model audit remain under its id. Deleting them is the only way to run that week
again with changed model settings, which the audit refuses under the same id.
"""

from __future__ import annotations

import re

from alerts_bi_shared.db.connection import Database

from .engine.api.jobs import lock_team_queue
from .engine.api.service import WeeklyBusy


class DeletionRefused(ValueError):
    """The run is missing or currently in use."""


def delete_run(db: Database, run_id: str, *, shared_lock: bool = True) -> str:
    """Delete in one SQL transaction; return the team for the post-delete redirect.

    The caller holds the HTTP run gate and normally the weekly schedule lock. The job worker
    holds that lock for a whole run, so without it (``shared_lock=False``) deletion goes
    ahead only beside a job holding the active slot for another run: while a job holds the
    slot, CLI runs and reports, the weekly schedule and synchronous HTTP runs refuse to
    start, so the lock's holder is the worker, and locking the job's row keeps it there
    until this transaction ends. The audit lock also excludes an active model review from a
    separate CLI process. Run-row locks serialize publication and deletion. Shared prompt
    artifacts, verdicts and panel parses survive.
    """
    if re.fullmatch(r"[a-f0-9]{64}", run_id) is None:
        raise DeletionRefused("Invalid run id.")
    params = {"run_id": run_id}
    with db.transaction():
        if (
            db.query_one(
                "SELECT 1 AS ready FROM schema_migrations WHERE version='009_run_deletion'"
            )
            is None
        ):
            raise DeletionRefused("Apply database migration 009_run_deletion before deleting runs.")
        active = db.query(
            "SELECT run_id FROM run_jobs WITH (UPDLOCK, HOLDLOCK) WHERE active_slot=1"
        )
        if not shared_lock and not active:
            raise WeeklyBusy("another run, report, weekly schedule or deletion is in progress")
        if any(row["run_id"] == run_id for row in active):
            raise DeletionRefused("This run is pending or active. Try deleting after it finishes.")
        locked = db.query_one(
            "DECLARE @result INT; EXEC @result = sys.sp_getapplock "
            "@Resource=:resource, @LockMode='Exclusive', @LockOwner='Transaction', @LockTimeout=0; "
            "SELECT @result AS result",
            {"resource": f"llm-review:{run_id}"},
        )
        if locked is None or int(locked["result"]) < 0:
            raise DeletionRefused("This run's model review is active. Try again when it finishes.")
        run = db.query_one(
            "SELECT team_id, status FROM runs WITH (UPDLOCK, HOLDLOCK) WHERE run_id=:run_id",
            params,
        )
        if run is None:
            # Never stored: the run's latest submission names the team.
            run = db.query_one(
                "SELECT TOP 1 team_id FROM run_jobs WITH (UPDLOCK, HOLDLOCK) "
                "WHERE run_id=:run_id ORDER BY submitted_at DESC, job_id DESC",
                params,
            )
        if run is None:
            raise DeletionRefused("No such run.")
        if run.get("status") == "running":
            raise DeletionRefused("This run is active. Try again when it finishes.")
        # Backfill and queued admission bypass the execution lock; the team's queue lock
        # makes this check and the job cleanup below atomic with respect to new queued rows.
        if not lock_team_queue(db, str(run["team_id"])):
            raise DeletionRefused("Runs for this team are being queued. Try deleting again.")
        if db.query_one(
            "SELECT TOP 1 job_id FROM run_jobs WHERE run_id=:run_id AND status='queued'", params
        ):
            raise DeletionRefused(
                "A queued run would recreate this week. Remove it from the queue before deleting."
            )
        db.execute(
            "DELETE a FROM llm_review_attempts a JOIN llm_review_batches b ON b.cycle_id=a.cycle_id "
            "JOIN llm_review_scopes s ON s.scope_id=b.scope_id "
            "WHERE s.scope_id=:run_id AND s.scope_kind='run'",
            params,
        )
        db.execute(
            "DELETE b FROM llm_review_batches b JOIN llm_review_scopes s ON s.scope_id=b.scope_id "
            "WHERE s.scope_id=:run_id AND s.scope_kind='run'",
            params,
        )
        db.execute(
            "DELETE FROM llm_review_scopes WHERE scope_id=:run_id AND scope_kind='run'", params
        )
        # The checked FK prevents a racing CLI/UI decision from creating an orphan.
        # Only this connection and exact run may remove decisions. Always clear the
        # allowance, including when the transaction fails; never disable the trigger.
        db.execute(
            "EXEC sys.sp_set_session_context @key=N'alerts_bi_delete_run', @value=:run_id", params
        )
        try:
            db.execute("DELETE FROM finding_decisions WHERE run_id=:run_id", params)
        finally:
            db.execute("EXEC sys.sp_set_session_context @key=N'alerts_bi_delete_run', @value=NULL")
        for table in (
            "run_analysis_checkpoints",
            "review_publications",
            "weekly_review_log",
            "daily_metrics",
            "daily_rule_counts",
            "alert_findings",
            "llm_batch_attempts",
            "run_panels",
            "run_jobs",
            "runs",
        ):
            db.execute(f"DELETE FROM {table} WHERE run_id=:run_id", params)
        return str(run["team_id"])
