"""What the admin app reads.

The admin app sees every run, while the portal queries only published weeks through its
views. Both use the application's SQL credential.
"""

from __future__ import annotations

from typing import Any

from alerts_bi_shared.db.connection import Database

__all__ = [
    "PAGE_SIZE",
    "alerts_with_findings",
    "decision_history",
    "team_log",
    "team_overview",
    "team_runs",
]

PAGE_SIZE = 50


def team_overview(db: Database) -> dict[str, dict[str, Any]]:
    """Per team: latest published week, weeks published, and the schedule's last outcome."""
    published = db.query(
        """
        SELECT team_id, MAX(window_end) AS latest, COUNT(*) AS weeks
        FROM review_publications WHERE withdrawn_at IS NULL GROUP BY team_id
        """
    )
    last = db.query(
        """
        SELECT l.team_id, l.outcome, l.window_end, l.detail, l.invoked_at
        FROM weekly_review_log AS l
        JOIN (SELECT team_id, MAX(log_id) AS log_id FROM weekly_review_log GROUP BY team_id)
          AS m ON m.log_id = l.log_id
        """
    )
    overview: dict[str, dict[str, Any]] = {}
    for row in published:
        overview.setdefault(str(row["team_id"]), {}).update(
            latest=row["latest"], weeks=int(row["weeks"])
        )
    for row in last:
        overview.setdefault(str(row["team_id"]), {})["last"] = row
    return overview


def team_runs(db: Database, team_id: str, limit: int = 60) -> list[dict[str, Any]]:
    """A team's runs, newest week first, with each one's publication state."""
    return db.query(
        """
        SELECT TOP (:limit)
               r.run_id, r.run_at, r.window_start, r.window_end, r.status, r.completed_at,
               r.model_version, r.llm_assessed, r.phase_derived, r.registry_version,
               r.ruleset_version, r.prompt_version,
               p.publication_id, p.published_at, p.published_by, p.review_note,
               (SELECT COUNT(*) FROM alert_findings f
                 WHERE f.run_id = r.run_id AND f.quality_state = 'unassessed') AS unassessed,
               (SELECT COUNT(*) FROM review_publications w
                 WHERE w.run_id = r.run_id AND w.withdrawn_at IS NOT NULL) AS withdrawn
        FROM runs AS r
        LEFT JOIN review_publications AS p
          ON p.run_id = r.run_id AND p.withdrawn_at IS NULL
        WHERE r.team_id = :team_id
        ORDER BY r.window_end DESC, r.completed_at DESC, r.run_id DESC
        """,
        {"team_id": team_id, "limit": limit},
    )


def team_log(db: Database, team_id: str, limit: int = 30) -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT TOP (:limit) invoked_at, window_end, outcome, run_id, detail
        FROM weekly_review_log WHERE team_id = :team_id ORDER BY log_id DESC
        """,
        {"team_id": team_id, "limit": limit},
    )


def alerts_with_findings(db: Database, run_id: str, page: int) -> tuple[list[dict[str, Any]], int]:
    """One page of a run's alerts that carry any finding, in the portal's work-list order."""
    where = (
        "run_id = :run_id AND (quality_state IN ('rule_flagged', 'llm_flagged', 'needs_review') "
        "OR readiness_rule_ids <> '')"
    )
    total_row = db.query_one(
        f"SELECT COUNT(*) AS n FROM alert_findings WHERE {where}", {"run_id": run_id}
    )
    total = int(total_row["n"]) if total_row else 0
    rows = db.query(
        f"""
        SELECT alert_schema, application, key_field, message, component, severity, row_count,
               core_rule_ids, readiness_rule_ids, quality_state, llm_principle_id,
               llm_confidence, llm_justification
        FROM alert_findings
        WHERE {where}
        ORDER BY CASE quality_state WHEN 'rule_flagged' THEN 0 WHEN 'llm_flagged' THEN 1
                 WHEN 'needs_review' THEN 2 ELSE 3 END, row_count DESC, alert_schema,
                 application, key_field
        OFFSET :offset ROWS FETCH NEXT :size ROWS ONLY
        """,
        {"run_id": run_id, "offset": (max(page, 1) - 1) * PAGE_SIZE, "size": PAGE_SIZE},
    )
    return rows, total


def decision_history(
    db: Database, identities: list[tuple[str, str, str]]
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """Every decision on these exact identities, oldest first."""
    history: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    if not identities:
        return history
    wanted = set(identities)
    applications = sorted({application for _, application, _ in identities})
    params = {f"a{i}": application for i, application in enumerate(applications)}
    names = ", ".join(f":a{i}" for i in range(len(applications)))
    rows = db.query(
        f"""
        SELECT alert_schema, application, key_field, finding_id, state, note, decided_at,
               decided_by
        FROM finding_decisions WHERE application IN ({names})
        ORDER BY decided_at ASC, decision_id ASC
        """,
        params,
    )
    for row in rows:
        key = (str(row["alert_schema"]), str(row["application"]), str(row["key_field"]))
        if key in wanted:
            history.setdefault(key, []).append(row)
    return history
