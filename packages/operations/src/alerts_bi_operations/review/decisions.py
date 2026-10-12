"""The human-review record on a finding (design section 7.10).

A decision is ``pending``, ``confirmed`` or ``dismissed``, with a note and a timestamp. It is
stored beside the finding and never changes it: the pipeline's ``quality_state`` and the
model's verdict stay exactly as the run wrote them, so a machine finding and a human decision
can always be told apart.

The record is append-only (a database trigger refuses updates and deletes): changing one's
mind is a new row, and readers see the whole history. A decision is keyed on the exact
identity and finding id, so it never carries over to the new v2 key a team mints by
enriching an alert.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from alerts_bi_shared.db.connection import Database

__all__ = [
    "DECISION_STATES",
    "DecisionRefused",
    "findings_on",
    "list_decisions",
    "record_decision",
    "resolve_published_run",
]

DECISION_STATES: Final = ("pending", "confirmed", "dismissed")
NOTE_LIMIT: Final = 2000


class DecisionRefused(ValueError):
    """The decision does not name a finding on a published week, or is malformed."""


def _split(ids: Any) -> list[str]:
    return [part for part in str(ids or "").split(",") if part]


def findings_on(row: dict[str, Any]) -> list[str]:
    """The finding ids a decision may name for one stored alert.

    Core rule findings, readiness gaps, and the model's cited principle when the model
    actually raised something (``llm_flagged`` or ``needs_review``). ``NONE`` is not a
    finding.
    """
    ids = [*_split(row.get("core_rule_ids")), *_split(row.get("readiness_rule_ids"))]
    principle = row.get("llm_principle_id")
    if row.get("quality_state") in ("llm_flagged", "needs_review") and principle:
        ids.append(str(principle))
    return ids


def resolve_published_run(
    db: Database, *, run_id: str | None, team_id: str | None, week: str | None
) -> str:
    """Name a published week either by run id, or by team and the date its week ends."""
    if run_id:
        row = db.query_one(
            "SELECT run_id FROM review_publications WHERE run_id = :run_id AND withdrawn_at IS NULL",
            {"run_id": run_id},
        )
        if row is None:
            raise DecisionRefused(f"run {run_id[:16]} is not a published week")
        return str(row["run_id"])
    if not team_id or not week:
        raise DecisionRefused("name the week with --run-id, or with --team and --week YYYY-MM-DD")
    row = db.query_one(
        """
        SELECT run_id FROM review_publications
        WHERE team_id = :team_id AND withdrawn_at IS NULL
          AND CAST(window_end AS DATE) = :week
        """,
        {"team_id": team_id, "week": week},
    )
    if row is None:
        raise DecisionRefused(f"{team_id} has no published week ending on {week}")
    return str(row["run_id"])


def record_decision(
    db: Database,
    *,
    run_id: str,
    alert_schema: str,
    application: str,
    key_field: str,
    finding_id: str,
    state: str,
    note: str,
    decided_by: str,
    now: datetime | None = None,
) -> int:
    """Append one decision on one finding of an alert in a published week."""
    if state not in DECISION_STATES:
        raise DecisionRefused(f"state must be one of {', '.join(DECISION_STATES)}; got {state!r}")
    cleaned = (note or "").strip()
    if not cleaned:
        raise DecisionRefused("a note is required: say why")
    if len(cleaned) > NOTE_LIMIT:
        raise DecisionRefused(f"the note is {len(cleaned)} characters; the limit is {NOTE_LIMIT}")
    by = (decided_by or "").strip()
    if not by:
        raise DecisionRefused("an operator name is required")
    moment = now or datetime.now(UTC)
    # Naive values are UTC throughout the pipeline, as the SQL Server driver hands them back.
    at = moment.astimezone(UTC).replace(tzinfo=None) if moment.tzinfo else moment

    with db.transaction():
        published = db.query_one(
            "SELECT team_id FROM review_publications "
            "WHERE run_id = :run_id AND withdrawn_at IS NULL",
            {"run_id": run_id},
        )
        if published is None:
            raise DecisionRefused(
                f"run {run_id[:16]} is not a published week; decisions are made on what "
                "readers can see"
            )
        row = db.query_one(
            """
            SELECT core_rule_ids, readiness_rule_ids, quality_state, llm_principle_id
            FROM alert_findings
            WHERE run_id = :run_id AND alert_schema = :alert_schema
              AND application = :application AND key_field = :key_field
            """,
            {
                "run_id": run_id,
                "alert_schema": alert_schema,
                "application": application,
                "key_field": key_field,
            },
        )
        if row is None:
            raise DecisionRefused("that alert is not in this week's review")
        available = findings_on(row)
        if finding_id not in available:
            listed = ", ".join(available) if available else "none"
            raise DecisionRefused(
                f"{finding_id!r} is not a finding on this alert; its findings are: {listed}"
            )

        inserted = db.query_one(
            """
            INSERT INTO finding_decisions
                (team_id, run_id, alert_schema, application, key_field, finding_id, state,
                 note, decided_at, decided_by)
            OUTPUT INSERTED.decision_id
            VALUES (:team_id, :run_id, :alert_schema, :application, :key_field, :finding_id,
                    :state, :note, :at, :by)
            """,
            {
                "team_id": published["team_id"],
                "run_id": run_id,
                "alert_schema": alert_schema,
                "application": application,
                "key_field": key_field,
                "finding_id": finding_id,
                "state": state,
                "note": cleaned,
                "at": at,
                "by": by,
            },
        )
        assert inserted is not None
        return int(inserted["decision_id"])


def list_decisions(db: Database, team_id: str) -> list[dict[str, Any]]:
    """A team's whole decision history, oldest first."""
    return db.query(
        """
        SELECT decision_id, run_id, alert_schema, application, key_field, finding_id, state,
               note, decided_at, decided_by
        FROM finding_decisions
        WHERE team_id = :team_id
        ORDER BY decided_at ASC, decision_id ASC
        """,
        {"team_id": team_id},
    )
