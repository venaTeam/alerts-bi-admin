"""Permanent deletion of a stored run and its own records, never shared caches."""

from __future__ import annotations

import re
from pathlib import Path

from alerts_bi_shared.db.connection import Database


class DeletionRefused(ValueError):
    """The run is missing or currently in use."""


def delete_run(db: Database, run_id: str) -> str:
    """Delete in one SQL transaction; return the team for the post-delete redirect.

    The caller holds the HTTP run gate and weekly schedule lock. The audit lock also
    excludes an active model review from a separate CLI process. Run-row locks serialize
    publication and deletion. Shared prompt artifacts, verdicts and panel parses survive.
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
            raise DeletionRefused("No such run.")
        if run["status"] == "running":
            raise DeletionRefused("This run is active. Try again when it finishes.")
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
            "review_publications",
            "weekly_review_log",
            "daily_metrics",
            "daily_rule_counts",
            "alert_findings",
            "llm_batch_attempts",
            "run_panels",
            "runs",
        ):
            db.execute(f"DELETE FROM {table} WHERE run_id=:run_id", params)
        return str(run["team_id"])


def remove_run_files(out_root: Path, run_id: str) -> None:
    """Remove the four generated reports in the configured manual and weekly locations.

    Never recurse through arbitrary files or follow a report-directory symlink outside
    its configured root. Explicit custom CLI output locations are not tracked in SQL.
    """
    if re.fullmatch(r"[a-f0-9]{64}", run_id) is None:
        raise ValueError("Invalid run id.")
    root = out_root.resolve()
    for parent in (root, root / "weekly"):
        directory = parent / run_id[:16]
        if directory.resolve() != directory or not directory.resolve().is_relative_to(root):
            raise OSError("The report directory points outside its configured location.")
        if not directory.exists():
            continue
        for name in (
            "scorecard.html",
            "daily_metrics.csv",
            "rule_counts.csv",
            "alert_worklist.csv",
        ):
            (directory / name).unlink(missing_ok=True)
        if not any(directory.iterdir()):
            directory.rmdir()
