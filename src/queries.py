"""What the admin app reads.

The admin app sees every run, while the portal queries only published weeks through its
views. Both use the application's SQL credential.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from alerts_bi_shared.db.connection import Database

__all__ = [
    "PAGE_SIZE",
    "alerts_with_findings",
    "all_decisions",
    "backfill_calendar",
    "decision_count",
    "decision_history",
    "first_run_end",
    "other_publication",
    "run_publications",
    "team_log",
    "team_overview",
    "team_runs",
    "undecided_count",
    "undecided_findings",
]

PAGE_SIZE = 50

# Every finding of a currently published week with no decision for that week. A finding is what
# findings_on() lets a decision name: each core rule id, each readiness gap, and the model's
# principle when the model raised something. STRING_SPLIT yields them in no set order, so the
# listing orders by the finding id last.
_UNDECIDED = """
    FROM review_publications AS p
    JOIN runs AS r ON r.run_id = p.run_id
    JOIN alert_findings AS f ON f.run_id = p.run_id
    CROSS APPLY STRING_SPLIT(CONCAT(f.core_rule_ids, ',', f.readiness_rule_ids, ',',
        CASE WHEN f.quality_state IN ('llm_flagged', 'needs_review') THEN f.llm_principle_id END),
        ',') AS s
    WHERE p.withdrawn_at IS NULL {team}
      AND (f.core_rule_ids <> '' OR f.readiness_rule_ids <> ''
           OR f.quality_state IN ('llm_flagged', 'needs_review'))
      AND s.value <> ''
      AND NOT EXISTS (
        SELECT 1 FROM finding_decisions AS d
        WHERE d.run_id = p.run_id AND d.alert_schema = f.alert_schema
          AND d.application = f.application AND d.key_field = f.key_field
          AND d.finding_id = s.value)
"""


def team_overview(db: Database) -> dict[str, dict[str, Any]]:
    """Per team: latest published week, the schedule's last outcome, the newest run with its
    publication state, and active (pending, queued or running) submissions."""
    published = db.query(
        """
        SELECT team_id, MAX(window_end) AS latest, COUNT(*) AS weeks
        FROM review_publications WHERE withdrawn_at IS NULL GROUP BY team_id
        """
    )
    last = db.query(
        """
        SELECT l.team_id, l.outcome, l.window_end, l.detail, l.invoked_at, l.run_id
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
    for row in db.query("""
        SELECT team_id, COUNT(*) AS run_count, MAX(run_at) AS latest_run
        FROM runs GROUP BY team_id
    """):
        overview.setdefault(str(row["team_id"]), {}).update(row)
    for row in db.query("""
        SELECT r.*, (SELECT COALESCE(CAST(JSON_VALUE(s.payload,'$.schemas.v1.states.unassessed') AS BIGINT),0)+COALESCE(CAST(JSON_VALUE(s.payload,'$.schemas.v2.states.unassessed') AS BIGINT),0) FROM run_summaries s WHERE s.run_id=r.run_id AND s.scope_kind='team') AS unassessed,
               (SELECT TOP 1 p.publication_id FROM review_publications p
                 WHERE p.run_id=r.run_id AND p.withdrawn_at IS NULL) AS publication_id,
               (SELECT COUNT(*) FROM review_publications w
                 WHERE w.run_id=r.run_id AND w.withdrawn_at IS NOT NULL) AS withdrawn,
               (SELECT TOP 1 l.outcome FROM weekly_review_log l
                 WHERE l.run_id=r.run_id ORDER BY l.log_id DESC) AS schedule_outcome
        FROM (SELECT *, ROW_NUMBER() OVER
          (PARTITION BY team_id ORDER BY run_at DESC, completed_at DESC, run_id DESC) AS rn
          FROM runs) r WHERE r.rn=1
    """):
        overview.setdefault(str(row["team_id"]), {})["run"] = row
    for row in db.query(
        "SELECT job_id, team_id, run_at, status, started_at, submitted_at FROM run_jobs "
        "WHERE status IN ('pending','queued','running') ORDER BY run_at DESC"
    ):
        overview.setdefault(str(row["team_id"]), {}).setdefault("active", []).append(row)
    return overview


def team_runs(db: Database, team_id: str, limit: int = 20, page: int = 1) -> list[dict[str, Any]]:
    """A team's runs, newest week first, with each one's publication state."""
    return db.query(
        """
        SELECT r.run_id, r.team_id, r.team_display_name, r.error_summary,
               r.run_at, r.window_start, r.window_end, r.status, r.completed_at,
               r.model_version, r.llm_assessed, r.phase_derived, r.registry_version,
               r.ruleset_version, r.prompt_version,
               p.publication_id, p.published_at, p.published_by, p.review_note,
               (SELECT COUNT(*) FROM weekly_review_log l WHERE l.run_id=r.run_id) AS scheduled,
               (SELECT COUNT(*) FROM run_jobs j WHERE j.run_id=r.run_id AND j.backfill_id IS NOT NULL
                 AND j.status='completed') AS backfilled,
               (SELECT COALESCE(CAST(JSON_VALUE(s.payload,'$.schemas.v1.states.unassessed') AS BIGINT),0)+COALESCE(CAST(JSON_VALUE(s.payload,'$.schemas.v2.states.unassessed') AS BIGINT),0) FROM run_summaries s WHERE s.run_id=r.run_id AND s.scope_kind='team') AS unassessed,
               (SELECT COUNT(*) FROM review_publications w
                 WHERE w.run_id = r.run_id AND w.withdrawn_at IS NOT NULL) AS withdrawn
        FROM runs AS r
        LEFT JOIN review_publications AS p
          ON p.run_id = r.run_id AND p.withdrawn_at IS NULL
        WHERE r.team_id = :team_id
        ORDER BY r.run_at DESC, r.completed_at DESC, r.run_id DESC
        OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY
        """,
        {"team_id": team_id, "limit": limit, "offset": (page - 1) * limit},
    )


def backfill_calendar(db: Database, team_id: str, earliest_end: datetime) -> dict[datetime, str]:
    """Each retained Monday window end's backfill state for the team.

    ``published`` and ``active`` (queued, pending or running) weeks are locked; ``completed``
    weeks have an unpublished run that a backfill replaces. Unlisted weeks have no run.
    """
    params = {"team_id": team_id, "earliest": earliest_end.replace(tzinfo=None)}
    states: dict[datetime, str] = {}
    for row in db.query(
        "SELECT DISTINCT window_end FROM runs WHERE team_id=:team_id AND status='completed' "
        "AND window_end>=:earliest",
        params,
    ):
        states[row["window_end"]] = "completed"
    for row in db.query(
        "SELECT DISTINCT run_at FROM run_jobs WHERE team_id=:team_id "
        "AND status IN ('queued','pending','running') AND run_at>=:earliest",
        params,
    ):
        states[row["run_at"]] = "active"
    for row in db.query(
        "SELECT DISTINCT window_end FROM review_publications WHERE team_id=:team_id "
        "AND withdrawn_at IS NULL AND window_end>=:earliest",
        params,
    ):
        states[row["window_end"]] = "published"
    return states


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
        f"SELECT COUNT(*) AS n FROM run_alert_list WHERE {where}", {"run_id": run_id}
    )
    total = int(total_row["n"]) if total_row else 0
    rows = db.query(
        f"""
        WITH selected AS (
            SELECT alert_schema,application,key_field,attention_rank,row_count
            FROM run_alert_list WHERE {where}
            ORDER BY attention_rank,row_count DESC,alert_schema,application,key_field
            OFFSET :offset ROWS FETCH NEXT :size ROWS ONLY
        )
        SELECT f.alert_schema,f.application,f.key_field,f.message,f.component,f.severity,f.environment,f.row_count,
               f.core_rule_ids,f.readiness_rule_ids,f.quality_state,f.llm_principle_id,
               f.llm_confidence,f.llm_justification,f.findings_evidence,f.representative_doc,
               f.first_seen,f.last_seen
        FROM selected s JOIN alert_findings f ON f.run_id=:run_id AND f.alert_schema=s.alert_schema
          AND f.application=s.application AND f.key_field=s.key_field
        ORDER BY s.attention_rank,s.row_count DESC,s.alert_schema,s.application,s.key_field
        """,
        {"run_id": run_id, "offset": (max(page, 1) - 1) * PAGE_SIZE, "size": PAGE_SIZE},
    )
    return rows, total


def decision_history(
    db: Database, identities: list[tuple[str, str, str]], team_id: str
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """Every decision on these exact identities, oldest first."""
    history: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    if not identities:
        return history
    wanted = set(identities)
    params = {"identities": json.dumps(identities)}
    rows = db.query(
        """
        SELECT alert_schema, application, key_field, finding_id, state, note, decided_at,
               run_id
        FROM finding_decisions d WHERE team_id = :team_id AND EXISTS (
          SELECT 1 FROM OPENJSON(:identities) WITH
          (s NVARCHAR(2) '$[0]', a NVARCHAR(256) '$[1]', k NVARCHAR(512) '$[2]') i
          WHERE i.s=d.alert_schema AND i.a=d.application AND i.k=d.key_field)
        ORDER BY decided_at ASC, decision_id ASC
        """,
        {**params, "team_id": team_id},
    )
    for row in rows:
        key = (str(row["alert_schema"]), str(row["application"]), str(row["key_field"]))
        if key in wanted:
            history.setdefault(key, []).append(row)
    return history


def run_decisions(db: Database, run_id: str, page: int = 1) -> tuple[list[dict[str, Any]], int]:
    total = db.query_one(
        "SELECT COUNT(*) AS n FROM finding_decisions WHERE run_id = :run_id", {"run_id": run_id}
    )
    rows = db.query(
        """
        SELECT alert_schema, application, key_field, finding_id, state, note, decided_at
        FROM finding_decisions WHERE run_id = :run_id
        ORDER BY decided_at DESC, decision_id DESC
        OFFSET :offset ROWS FETCH NEXT :size ROWS ONLY
    """,
        {"run_id": run_id, "offset": (page - 1) * PAGE_SIZE, "size": PAGE_SIZE},
    )
    return rows, int(total["n"]) if total else 0


def all_decisions(
    db: Database, page: int = 1, team: str = "", state: str = ""
) -> tuple[list[dict[str, Any]], int]:
    """Decisions across teams and runs, newest first, optionally for one team or state."""
    where: list[str] = []
    params: dict[str, Any] = {"offset": (page - 1) * PAGE_SIZE, "size": PAGE_SIZE}
    if team:
        where.append("d.team_id = :team")
        params["team"] = team
    if state:
        where.append("d.state = :state")
        params["state"] = state
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.query_one(f"SELECT COUNT(*) AS n FROM finding_decisions d {clause}", params)
    rows = db.query(
        f"""
        SELECT d.*, r.team_display_name, r.window_start, r.window_end
        FROM finding_decisions d JOIN runs r ON r.run_id=d.run_id {clause}
        ORDER BY d.decided_at DESC, d.decision_id DESC
        OFFSET :offset ROWS FETCH NEXT :size ROWS ONLY
    """,
        params,
    )
    return rows, int(total["n"]) if total else 0


def decision_count(db: Database, team: str = "") -> int:
    """How many decisions are recorded, optionally for one team."""
    clause, params = ("WHERE team_id = :team", {"team": team}) if team else ("", {})
    row = db.query_one(f"SELECT COUNT(*) AS n FROM finding_decisions {clause}", params)
    return int(row["n"]) if row else 0


def undecided_count(db: Database, team: str = "") -> int:
    """How many findings of published weeks have no decision, optionally for one team."""
    clause, params = ("AND p.team_id = :team", {"team": team}) if team else ("", {})
    row = db.query_one("SELECT COUNT(*) AS n " + _UNDECIDED.format(team=clause), params)
    return int(row["n"]) if row else 0


def undecided_findings(
    db: Database, page: int = 1, team: str = ""
) -> tuple[list[dict[str, Any]], int]:
    """Findings of published weeks with no decision: newest week first, then team, then the
    alerts with the most events."""
    clause = "AND p.team_id = :team" if team else ""
    params: dict[str, Any] = {"offset": (page - 1) * PAGE_SIZE, "size": PAGE_SIZE}
    if team:
        params["team"] = team
    rows = db.query(
        """
        SELECT r.team_id, r.team_display_name, r.window_start, r.window_end, p.published_at,
               f.run_id, f.alert_schema, f.application, f.key_field, f.component, f.row_count,
               f.quality_state, f.llm_principle_id, f.llm_justification, f.findings_evidence,
               f.core_rule_ids, f.readiness_rule_ids, s.value AS finding_id
        """
        + _UNDECIDED.format(team=clause)
        + """
        ORDER BY r.window_end DESC, r.team_display_name, r.team_id, f.row_count DESC,
                 f.alert_schema, f.application, f.key_field, s.value
        OFFSET :offset ROWS FETCH NEXT :size ROWS ONLY
        """,
        params,
    )
    return rows, undecided_count(db, team)


def first_run_end(db: Database, team_id: str) -> datetime | None:
    """When the team's earliest stored run window ended, if it has any run."""
    row = db.query_one(
        "SELECT MIN(window_end) AS ended FROM runs WHERE team_id=:team_id", {"team_id": team_id}
    )
    return row["ended"] if row and row["ended"] is not None else None


def run_publications(db: Database, run_id: str) -> list[dict[str, Any]]:
    """Every publication of a run, current and withdrawn, newest first."""
    return db.query(
        "SELECT * FROM review_publications WHERE run_id=:run_id ORDER BY publication_id DESC",
        {"run_id": run_id},
    )


def other_publication(db: Database, team_id: str, window_end: datetime, run_id: str) -> bool:
    """Whether another run of the same team and week is currently published."""
    return (
        db.query_one(
            "SELECT TOP 1 run_id FROM review_publications WHERE team_id=:team_id "
            "AND window_end=:window_end AND run_id<>:run_id AND withdrawn_at IS NULL",
            {"team_id": team_id, "window_end": window_end, "run_id": run_id},
        )
        is not None
    )


def finding_page(db: Database, run_id: str, key: str) -> int:
    """Locate an exact finding from a decision link, preserving the SQL list order."""
    from alerts_bi_operations.review.decisions import findings_on

    from .pages import finding_key

    rows = db.query(
        """
        SELECT alert_schema, application, key_field, core_rule_ids, readiness_rule_ids,
               quality_state, llm_principle_id
        FROM alert_findings WHERE run_id=:run_id AND
          (quality_state IN ('rule_flagged', 'llm_flagged', 'needs_review') OR readiness_rule_ids <> '')
        ORDER BY CASE quality_state WHEN 'rule_flagged' THEN 0 WHEN 'llm_flagged' THEN 1
          WHEN 'needs_review' THEN 2 ELSE 3 END, row_count DESC, alert_schema, application, key_field
    """,
        {"run_id": run_id},
    )
    for index, row in enumerate(rows):
        if any(finding_key(row, finding) == key for finding in findings_on(row)):
            return index // PAGE_SIZE + 1
    return 1
