"""Read exact aggregates and bounded examples through an explicit SQL surface."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any, Literal

from alerts_bi_shared.db.connection import Database
from alerts_bi_shared.insights.model import AlertRow, WeekRules
from alerts_bi_shared.insights.stored import alert_from_row, merge_facts


@lru_cache(maxsize=64)
def _decode(payload: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(payload)
    if result.get("version") != 1:
        raise ValueError("Unsupported stored summary format")
    return result


def decode_facts(payload: str) -> dict[str, Any]:
    # Cache only immutable serialized facts, never publication visibility or decisions.
    # merge_facts allocates every mutable output; callers cannot mutate the cached value.
    if len(payload) <= 262144:
        return _decode(payload)
    result: dict[str, Any] = json.loads(payload)
    if result.get("version") != 1:
        raise ValueError("Unsupported stored summary format")
    return result


def selection(
    applications: tuple[str, ...] | None, column: str = "application"
) -> tuple[str, dict[str, Any]]:
    if applications is None:
        return "1=1", {}
    if not applications:
        return "1=0", {}
    # One parameter avoids SQL Server's parameter limit for large selections.
    return f"{column} IN (SELECT value FROM OPENJSON(:applications))", {
        "applications": json.dumps(applications)
    }


def read_summary(
    db: Database,
    run_id: str,
    surface: Literal["admin", "portal"],
    applications: tuple[str, ...] | None = None,
) -> tuple[dict[str, Any], tuple[AlertRow, ...], list[dict[str, Any]]]:
    summary_table = "portal_run_summaries" if surface == "portal" else "run_summaries"
    listing = "portal_alert_list" if surface == "portal" else "run_alert_list"
    daily_table = "portal_application_daily" if surface == "portal" else "run_application_daily"
    clause, app_params = selection(applications)
    params = {"run": run_id, **app_params}
    scope = (
        "scope_kind='team'" if applications is None else f"scope_kind='application' AND {clause}"
    )
    parts = db.query(f"SELECT payload FROM {summary_table} WHERE run_id=:run AND {scope}", params)
    facts = merge_facts([decode_facts(p["payload"]) for p in parts])
    # Every subset has its own bounded examples. Their count is never used as a total.
    categories = [
        "1=1",
        "(','+core_rule_ids+',') LIKE '%,R5,%'",
        "unseen=1",
        "alert_schema='v2' AND LOWER(severity)='critical' AND (','+readiness_rule_ids+',') LIKE '%,R9,%'",
    ]
    selects = []
    for schema in ("v1", "v2"):
        for category in categories:
            selects.append(
                f"SELECT * FROM (SELECT TOP 9 * FROM {listing} WHERE run_id=:run "
                f"AND alert_schema='{schema}' AND {clause} AND ({category}) "
                "ORDER BY row_count DESC, application, key_field) AS sample"
            )
    selects.append(
        f"SELECT * FROM (SELECT TOP 1 * FROM {listing} WHERE run_id=:run AND {clause} "
        "ORDER BY row_count DESC,alert_schema,key_field,application) AS sample"
    )
    samples = db.query(" UNION ALL ".join(selects), params)
    unique = {(r["alert_schema"], r["application"], r["key_field"]): r for r in samples}
    alerts = tuple(alert_from_row(r) for r in unique.values())
    concentration = db.query(
        f"WITH histogram AS (SELECT alert_schema,row_count,COUNT_BIG(*) AS n FROM {listing} "
        f"WHERE run_id=:run AND {clause} GROUP BY alert_schema,row_count), cumulative AS ("
        "SELECT *,SUM(n) OVER(PARTITION BY alert_schema) AS total_n,"
        "SUM(n*CAST(row_count AS BIGINT)) OVER(PARTITION BY alert_schema) AS total_events,"
        "SUM(n) OVER(PARTITION BY alert_schema ORDER BY row_count DESC ROWS UNBOUNDED PRECEDING) AS cum_n,"
        "SUM(n*CAST(row_count AS BIGINT)) OVER(PARTITION BY alert_schema ORDER BY row_count DESC ROWS UNBOUNDED PRECEDING) AS cum_events "
        "FROM histogram) SELECT * FROM cumulative WHERE cum_events*5 >= total_events*4 "
        "AND (cum_events-n*row_count)*5 < total_events*4",
        params,
    )
    for row in concentration:
        before = int(row["cum_events"]) - int(row["n"]) * int(row["row_count"])
        required = (4 * int(row["total_events"]) + 4) // 5
        additional = (required - before + int(row["row_count"]) - 1) // int(row["row_count"])
        facts["schemas"][row["alert_schema"]]["concentration"] = [
            int(row["cum_n"]) - int(row["n"]) + additional,
            before + additional * int(row["row_count"]),
        ]
    daily = (
        db.query(
            "SELECT alert_schema,snapshot_date,MAX(covered_hours) AS covered_hours,"
            "SUM(events) AS events,SUM(distinct_alerts) AS distinct_alerts,SUM(flagged_rows) AS flagged_rows,"
            "SUM(flagged_by_rule_distinct) AS flagged_by_rule_distinct,SUM(suppressed) AS suppressed,"
            f"SUM(unseen) AS unseen FROM {daily_table} WHERE run_id=:run AND {clause} "
            "GROUP BY alert_schema,snapshot_date ORDER BY alert_schema,snapshot_date",
            params,
        )
        if applications is not None
        else []
    )
    return facts, alerts, daily


def read_history(
    db: Database,
    team_id: str,
    run_id: str,
    window_end: datetime,
    surface: Literal["admin", "portal"],
    applications: tuple[str, ...] | None,
    facts: dict[str, Any],
    *,
    published: bool,
) -> tuple[WeekRules, ...]:
    rules = "portal_v1_rules" if surface == "portal" else "run_v1_rules"
    clause, app_params = selection(applications)
    params = {"run": run_id, **app_params}
    count = db.query_one(
        f"SELECT COUNT_BIG(DISTINCT rule_hash) AS n FROM {rules} WHERE run_id=:run AND {clause}",
        params,
    )
    facts["rules_left"] = int(count["n"]) if count else 0
    if not published:
        return ()
    weeks = list(
        reversed(
            db.query(
                "SELECT TOP 4 run_id,window_end,basis_changed FROM portal_reviews "
                "WHERE team_id=:team AND window_end<=:end ORDER BY window_end DESC",
                {"team": team_id, "end": window_end},
            )
        )
    )
    earlier: list[str] = []
    for i in range(len(weeks) - 2, -1, -1):
        if weeks[i + 1]["basis_changed"] or weeks[i + 1]["window_end"] - weeks[i][
            "window_end"
        ] != timedelta(days=7):
            break
        earlier.append(str(weeks[i]["run_id"]))
    if earlier:
        count = db.query_one(
            "SELECT COUNT_BIG(*) AS n FROM ("
            f"SELECT rule_hash FROM {rules} WHERE run_id IN (SELECT value FROM OPENJSON(:earlier)) AND {clause} "
            f"EXCEPT SELECT rule_hash FROM {rules} WHERE run_id=:run AND {clause}) retired",
            {**params, "earlier": json.dumps(earlier)},
        )
        facts["retired"] = int(count["n"]) if count else 0
    return tuple(WeekRules(w["window_end"], frozenset(), bool(w["basis_changed"])) for w in weeks)
