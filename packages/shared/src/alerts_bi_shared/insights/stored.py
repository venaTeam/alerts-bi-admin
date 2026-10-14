"""Versioned compact summary facts. No application or engine imports.

Totals always describe the complete selection; ``alerts`` are bounded examples only.
The original full-input algorithms remain available as the parity oracle.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from alerts_bi_shared.catalogs import CORE_RULE_IDS
from alerts_bi_shared.insights.model import AlertRow, AppRow, SchemaTotals, SummaryInputs


def alert_from_row(row: Mapping[str, Any]) -> AlertRow:
    values: dict[str, Any] = {
        name: row.get(name)
        for name in (
            "application",
            "key_field",
            "message",
            "severity",
            "provider",
            "alert_rule_url",
            "component",
            "node_name",
            "row_count",
            "first_seen",
            "last_seen",
            "quality_state",
            "llm_principle_id",
            "llm_confidence",
            "clear_count",
            "max_clear_cycles_24h",
            "fire_pattern",
            "unseen",
            "max_episode_firing_rows",
            "open_since",
        )
    }
    for name in ("clear_count", "max_clear_cycles_24h", "max_episode_firing_rows"):
        values[name] = int(values[name] or 0)
    return AlertRow(
        schema=str(row["alert_schema"]),
        **values,
        core_rule_ids=tuple(filter(None, str(row.get("core_rule_ids") or "").split(","))),
        readiness_rule_ids=tuple(filter(None, str(row.get("readiness_rule_ids") or "").split(","))),
    )


def build_facts(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Called once during persistence/backfill, never by a page request."""
    from alerts_bi_shared.insights.aggregate import by_application

    alerts = tuple(alert_from_row(r) for r in rows)
    result: dict[str, Any] = {"version": 1, "schemas": {}, "by_application": []}
    result["by_application"] = [
        {**asdict(a), "rules": list(a.rules)} for a in by_application(alerts)
    ]
    for schema in ("v1", "v2"):
        members = [a for a in alerts if a.schema == schema]
        states = Counter(a.quality_state for a in members)
        primary: Counter[str] = Counter()
        patterns: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        model: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        combinations: dict[str, set[str]] = defaultdict(set)
        rule_apps: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        rules: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        import json

        for row, alert in zip(rows, alerts, strict=True):
            if alert.schema != schema:
                continue
            evidence = {e["rule_id"]: e for e in json.loads(row.get("findings_evidence") or "[]")}
            for rid in (*alert.core_rule_ids, *alert.readiness_rule_ids):
                rules[rid][0] += int(evidence.get(rid, {}).get("matched_rows", 0))
                rules[rid][1] += 1
                rule_apps[rid][alert.application] += alert.row_count
            for rid in alert.core_rule_ids:
                combinations[rid].add(",".join(sorted(set(alert.core_rule_ids) - {rid})))
            if alert.quality_state == "rule_flagged":
                first_rule = next((r for r in CORE_RULE_IDS if r in alert.core_rule_ids), None)
                if first_rule:
                    primary[first_rule] += 1
            if alert.fire_pattern:
                patterns[alert.fire_pattern][0] += 1
                patterns[alert.fire_pattern][1] += alert.row_count
            if alert.quality_state == "llm_flagged" and alert.llm_principle_id:
                model[alert.llm_principle_id][0] += 1
                model[alert.llm_principle_id][1] += alert.row_count
        result["schemas"][schema] = {
            "events": sum(a.row_count for a in members),
            "distinct_alerts": len(members),
            "states": dict(states),
            "readiness_gaps": sum(bool(a.readiness_rule_ids) for a in members),
            "needs_attention": sum(
                a.quality_state in ("rule_flagged", "llm_flagged", "needs_review")
                or bool(a.readiness_rule_ids)
                for a in members
            ),
            "unseen_alerts": sum(bool(a.unseen) for a in members)
            if any(a.unseen is not None for a in members)
            else None,
            "applications": len({a.application for a in members}),
            "ready": sum(
                not (set(a.readiness_rule_ids) & {"R8", "R10"})
                and not ((a.severity or "").lower() == "critical" and "R9" in a.readiness_rule_ids)
                for a in members
            ),
            "critical": sum(
                (a.severity or "").lower() == "critical" and "R9" in a.readiness_rule_ids
                for a in members
            ),
            "rules": dict(rules),
            "primary": dict(primary),
            "patterns": dict(patterns),
            "model": dict(model),
            "combinations": {r: sorted(v) for r, v in combinations.items()},
            "rule_apps": dict(rule_apps),
        }
    return result


def merge_facts(parts: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Merge disjoint applications; never merge team and application scopes."""
    result: dict[str, Any] = {"version": 1, "schemas": {}, "by_application": []}
    for schema in ("v1", "v2"):
        total: dict[str, Any] = dict.fromkeys(
            (
                "events",
                "distinct_alerts",
                "readiness_gaps",
                "needs_attention",
                "applications",
                "ready",
                "critical",
            ),
            0,
        )
        total.update(
            {
                k: {}
                for k in (
                    "states",
                    "primary",
                    "rules",
                    "patterns",
                    "model",
                    "combinations",
                    "rule_apps",
                )
            }
        )
        total["unseen_alerts"] = None
        for part in parts:
            item = part["schemas"][schema]
            for key in (
                "events",
                "distinct_alerts",
                "readiness_gaps",
                "needs_attention",
                "applications",
                "ready",
                "critical",
            ):
                total[key] += item[key]
            if item["unseen_alerts"] is not None:
                total["unseen_alerts"] = (total["unseen_alerts"] or 0) + item["unseen_alerts"]
            for kind in ("states", "primary"):
                for key, n in item[kind].items():
                    total[kind][key] = total[kind].get(key, 0) + n
            for kind in ("rules", "patterns", "model"):
                for key, values in item[kind].items():
                    pair = total[kind].setdefault(key, [0, 0])
                    pair[0] += values[0]
                    pair[1] += values[1]
            for rule, values in item["combinations"].items():
                total["combinations"][rule] = sorted(
                    set(total["combinations"].get(rule, [])) | set(values)
                )
            for rule, apps in item["rule_apps"].items():
                total["rule_apps"].setdefault(rule, {}).update(apps)
        result["schemas"][schema] = total
    result["by_application"] = sorted(
        [{**a, "rules": list(a["rules"])} for p in parts for a in p["by_application"]],
        key=lambda a: (
            -(a["rule_flagged_events"] + a["llm_flagged_events"]),
            -a["events"],
            a["application"],
            a["schema"],
        ),
    )
    return result


def fact(inputs: SummaryInputs, schema: str, key: str, fallback: Any = 0) -> Any:
    return inputs.facts["schemas"][schema].get(key, fallback) if inputs.facts else fallback


def patterns(inputs: SummaryInputs, schema: str | None = None) -> dict[str, list[int]]:
    """Exact pattern identity/event counts, including the full-input reference path."""
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    if inputs.facts is not None:
        for name in (schema,) if schema else ("v1", "v2"):
            for pattern, pair in inputs.facts["schemas"][name]["patterns"].items():
                counts[pattern][0] += pair[0]
                counts[pattern][1] += pair[1]
    else:
        for alert in inputs.alerts:
            if alert.fire_pattern and (schema is None or alert.schema == schema):
                counts[alert.fire_pattern][0] += 1
                counts[alert.fire_pattern][1] += alert.row_count
    return counts


def application_rows(inputs: SummaryInputs) -> tuple[AppRow, ...]:
    from alerts_bi_shared.insights.aggregate import by_application

    if inputs.facts is None:
        return by_application(inputs.alerts)
    return tuple(
        AppRow(**{**a, "rules": tuple(a["rules"])}) for a in inputs.facts["by_application"]
    )


def schema_totals(
    facts: dict[str, Any], daily: Sequence[Mapping[str, Any]], *, admin: bool
) -> dict[str, SchemaTotals]:
    result = {}
    for schema in ("v1", "v2"):
        f = facts["schemas"][schema]
        days = [d for d in daily if d["alert_schema"] == schema]
        measured = bool(days)
        unseen = [int(d["unseen"]) for d in days if d.get("unseen") is not None]
        result[schema] = SchemaTotals(
            schema=schema,
            events=sum(int(d["alerts"]) for d in days) if admin else f["events"],
            distinct_alerts=f["distinct_alerts"],
            distinct_per_day=sum(d["distinct_alerts"] for d in days) / 7 if admin else None,
            rule_flagged_events=sum(
                d.get("flagged_by_rule", d.get("flagged_rows", 0)) for d in days
            )
            if measured or admin
            else (0 if not f["distinct_alerts"] else None),
            rule_flagged_alerts=f["states"].get("rule_flagged", 0),
            suppressed=sum(d["suppressed"] for d in days)
            if measured
            else f["rules"].get("R5", [0])[0],
            unseen=sum(unseen) if unseen else None,
            unseen_alerts=f["unseen_alerts"],
            states=f["states"],
            readiness_gaps=f["readiness_gaps"],
            suppression_unmeasured=sum(d.get("suppression_unmeasured", 0) for d in days)
            if admin
            else None,
            unseen_unmeasured=sum(d.get("unseen_unmeasured") or 0 for d in days)
            if admin and unseen
            else None,
        )
    return result
