"""Read committed run rows for operator scorecards and exports."""

from __future__ import annotations

from typing import Any

from alerts_bi_shared.db.connection import Database


def get_run(db: Database, run_id: str) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM runs WHERE run_id = :run_id", {"run_id": run_id})


def get_latest_run(db: Database, team_id: str) -> dict[str, Any] | None:
    """Most recent completed run for a team, used when re-rendering without a run id.

    ``run_at`` alone does not order these. A team is routinely re-run over the same frozen
    clock - after a registry edit, a ruleset bump or a code change - and each of those is a
    distinct run with the same ``run_at``. Ordering by ``run_at`` alone leaves such runs
    tied, and a tie in SQL Server resolves to whichever row the engine happens to return,
    so "latest" could silently mean the oldest. ``completed_at`` breaks the tie by actual
    execution, and ``run_id`` makes the result total rather than merely usually right.
    """
    return db.query_one(
        "SELECT TOP 1 * FROM runs WHERE team_id = :team_id AND status = 'completed' "
        "ORDER BY run_at DESC, completed_at DESC, run_id DESC",
        {"team_id": team_id},
    )


def get_daily_metrics(db: Database, run_id: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT * FROM daily_metrics WHERE run_id = :run_id "
        "ORDER BY alert_schema ASC, snapshot_date ASC",
        {"run_id": run_id},
    )


def get_rule_counts(db: Database, run_id: str) -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT * FROM daily_rule_counts
        WHERE run_id = :run_id
        ORDER BY alert_schema ASC, snapshot_date ASC,
                 CAST(SUBSTRING(rule_id, 2, 8) AS INT) ASC
        """,
        {"run_id": run_id},
    )


def get_findings(db: Database, run_id: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT * FROM alert_findings WHERE run_id = :run_id "
        "ORDER BY alert_schema ASC, application ASC, key_field ASC",
        {"run_id": run_id},
    )


def get_batch_attempts(db: Database, run_id: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT * FROM llm_batch_attempts WHERE run_id = :run_id "
        "ORDER BY batch_id ASC, attempt_number ASC",
        {"run_id": run_id},
    )


def get_run_panels(db: Database, run_id: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT * FROM run_panels WHERE run_id = :run_id ORDER BY panel_id ASC",
        {"run_id": run_id},
    )
