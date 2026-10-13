"""Produce compact SQL read models inside the caller's completed-run transaction."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any

from alerts_bi_shared.db.connection import Database
from alerts_bi_shared.insights.estimate import v1_rule_key_of
from alerts_bi_shared.insights.stored import build_facts

from .bulk import insert_rows

LIST_COLUMNS = (
    "run_id, alert_schema, application, key_field, message, severity, provider, alert_rule_url, "
    "component, node_name, row_count, first_seen, last_seen, quality_state, core_rule_ids, "
    "readiness_rule_ids, llm_principle_id, llm_confidence, clear_count, max_clear_cycles_24h, "
    "fire_pattern, unseen, max_episode_firing_rows, open_since"
)


def rebuild_read_models(
    db: Database, run_id: str, findings: Sequence[Mapping[str, Any]] | None = None
) -> None:
    rows = (
        list(findings)
        if findings is not None
        else db.query("SELECT * FROM alert_findings WHERE run_id=:run", {"run": run_id})
    )
    for table in ("run_summaries", "run_alert_list", "run_v1_rules"):
        db.execute(f"DELETE FROM {table} WHERE run_id=:run", {"run": run_id})
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row["application"])].append(row)
    scopes = [
        ("team", "", rows),
        *(("application", app, members) for app, members in groups.items()),
    ]
    for scope, app, members in scopes:
        facts = build_facts(members)
        if scope == "team":
            # Daily matches include older readiness failures even when the latest
            # representative is ready. Keep that event count separate from identities.
            for rule in db.query(
                "SELECT alert_schema,rule_id,SUM(CAST(match_count AS BIGINT)) AS events "
                "FROM daily_rule_counts WHERE run_id=:run GROUP BY alert_schema,rule_id",
                {"run": run_id},
            ):
                rules = facts["schemas"][rule["alert_schema"]]["rules"]
                rules.setdefault(rule["rule_id"], [0, 0])[0] = int(rule["events"])
        db.execute(
            "INSERT INTO run_summaries(run_id,scope_kind,application,payload) VALUES (:run,:scope,:app,:payload)",
            {
                "run": run_id,
                "scope": scope,
                "app": app,
                "payload": json.dumps(facts, ensure_ascii=False, separators=(",", ":")),
            },
        )
    select_columns = LIST_COLUMNS.replace("key_field, message,", "key_field, LEFT(message,512),")
    db.execute(
        f"INSERT INTO run_alert_list ({LIST_COLUMNS},attention_rank) SELECT {select_columns}, "
        "CASE quality_state WHEN 'rule_flagged' THEN 0 WHEN 'llm_flagged' THEN 1 "
        "WHEN 'needs_review' THEN 2 ELSE CASE WHEN readiness_rule_ids<>'' THEN 3 ELSE 4 END END "
        "FROM alert_findings WHERE run_id=:run",
        {"run": run_id},
    )
    db.execute(
        "INSERT INTO run_rule_membership(finding_id,run_id,rule_id,matched_rows) "
        "SELECT l.finding_id,l.run_id,r.value,COALESCE(j.matched_rows,0) FROM run_alert_list l "
        "JOIN alert_findings f ON f.run_id=l.run_id AND f.alert_schema=l.alert_schema "
        "AND f.application=l.application AND f.key_field=l.key_field "
        "CROSS APPLY STRING_SPLIT(l.core_rule_ids+','+l.readiness_rule_ids,',') r "
        "OUTER APPLY (SELECT matched_rows FROM OPENJSON(f.findings_evidence) "
        "WITH (rule_id NVARCHAR(8),matched_rows BIGINT) e WHERE e.rule_id=r.value) j "
        "WHERE l.run_id=:run AND r.value<>''",
        {"run": run_id},
    )
    keys = {
        (str(r["application"]), v1_rule_key_of(str(r["application"]), r.get("alert_rule_url")))
        for r in rows
        if r["alert_schema"] == "v1"
    }
    insert_rows(
        db,
        "run_v1_rules",
        ("run_id", "application", "rule_hash"),
        [
            {"run_id": run_id, "application": app, "rule_hash": sha256(key.encode()).digest()}
            for app, key in sorted(keys)
        ],
    )
