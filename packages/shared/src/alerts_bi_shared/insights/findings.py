"""Key-findings sentences (spec section 4): templated from stored counts, never model text.

A sentence names a problem in plain words and never its rule id (product owner, 2026-10-04);
the id travels only in :attr:`KeyFinding.rule_filter`, for the link to the alerts."""

from __future__ import annotations

from collections import defaultdict

from alerts_bi_shared.insights.labels import problem
from alerts_bi_shared.insights.model import AlertRow, KeyFinding, SummaryInputs

_CORE_RULES = ("R1", "R2", "R3", "R4", "R5", "R6", "R7")
_MAX_FINDINGS = 5
_SCHEMAS = ("v1", "v2")


def _count(n: int, noun: str) -> str:
    """``1 v1 alert``, ``2 v1 alerts``, ``1,000 events``."""
    return f"{n:,} {noun}{'' if n == 1 else 's'}"


def _verb(parts: list[tuple[int, str]], singular: str, plural: str) -> str:
    """Agree with a list of counts: singular only for exactly one thing."""
    return singular if len(parts) == 1 and parts[0][0] == 1 else plural


def _joined(parts: list[tuple[int, str]]) -> str:
    return " and ".join(text for _, text in parts)


def _largest(inputs: SummaryInputs) -> KeyFinding | None:
    events: dict[str, int] = defaultdict(int)
    for rule in inputs.rules:
        if rule.rule_id in _CORE_RULES:
            events[rule.rule_id] += rule.events
    events = {rid: n for rid, n in events.items() if n > 0}
    if not events:
        return None
    rid = min(events, key=lambda r: (-events[r], r))
    # Describe the schema where the rule is biggest; v1 wins a tie.
    per_schema = {r.schema: r for r in inputs.rules if r.rule_id == rid}
    schema = min(per_schema, key=lambda s: (-per_schema[s].events, s))
    total = per_schema[schema]
    body = f"{_count(total.events, f'{schema} event')} from {_count(total.alerts, 'alert')}."
    carriers = [a for a in inputs.alerts if a.schema == schema and rid in a.core_rule_ids]
    others = {tuple(sorted(set(a.core_rule_ids) - {rid})) for a in carriers}
    if inputs.facts is not None:
        others = {
            tuple(filter(None, value.split(",")))
            for value in inputs.facts["schemas"][schema]["combinations"].get(rid, [])
        }
    if total.alerts and len(others) == 1 and len(next(iter(others))) == 1:
        body += f" The same alerts also have: {problem(next(iter(others))[0]).lower()}."
    return KeyFinding("largest", f"Biggest problem: {problem(rid).lower()}", body, None, rid)


def _unassessed(inputs: SummaryInputs) -> KeyFinding | None:
    """Per schema: v1 and v2 are never added together."""
    parts = [
        (count, _count(count, f"{schema} alert"))
        for schema in _SCHEMAS
        if schema in inputs.schemas
        and (count := inputs.schemas[schema].states.get("unassessed", 0)) > 0
    ]
    if not parts:
        return None
    return KeyFinding(
        "unassessed",
        f"{_joined(parts)} could not be classified",
        "Unassessed should be zero. A non-zero count is a classifier failure, not a finding.",
        None,
        None,
    )


def _concentration(inputs: SummaryInputs) -> KeyFinding | None:
    # The schema with the most events; v1 wins a tie.
    schema = min(inputs.schemas, key=lambda s: (-inputs.schemas[s].events, s))
    alerts = sorted(
        (a for a in inputs.alerts if a.schema == schema),
        key=lambda a: (-a.row_count, a.application, a.key_field),
    )
    n = len(alerts)
    total = sum(a.row_count for a in alerts)
    if inputs.facts is not None:
        n = inputs.schemas[schema].distinct_alerts
        total = inputs.schemas[schema].events
    if n <= 2 or total <= 0:
        return None
    acc = 0
    k = 0
    for k, alert in enumerate(alerts, start=1):
        acc += alert.row_count
        if acc * 5 >= total * 4 or k == n:
            break
    if inputs.facts is not None:
        k, acc = inputs.facts["schemas"][schema].get("concentration", [0, 0])
    if k >= n:
        return None
    pct = int(100 * acc / total + 0.5)
    return KeyFinding(
        "concentration",
        f"{k} of {n} {schema} alerts make {pct}% of the events",
        f"They produced {acc:,} of {total:,} {schema} events this week.",
        None,
        None,
    )


def _hidden(inputs: SummaryInputs) -> KeyFinding | None:
    parts = [
        (inputs.schemas[s].suppressed, _count(inputs.schemas[s].suppressed, f"{s} event"))
        for s in _SCHEMAS
        if s in inputs.schemas and inputs.schemas[s].suppressed > 0
    ]
    if not parts:
        return None
    verb = _verb(parts, "matches", "match")
    return KeyFinding(
        "hidden",
        "Your own panels hide alerts you still send",
        f"{_joined(parts)} {verb} a filter in your dashboard.",
        None,
        "R5",
    )


def _unseen(inputs: SummaryInputs) -> KeyFinding | None:
    shown = [s for s in inputs.schemas.values() if (s.unseen_alerts or 0) > 0]
    if not shown:
        return None
    parts = [
        (
            s.unseen_alerts or 0,
            f"{_count(s.unseen_alerts or 0, f'{s.schema} alert')}"
            + (f" ({_count(s.unseen, 'event')})" if s.unseen is not None else ""),
        )
        for s in sorted(shown, key=lambda t: t.schema)
    ]
    verb = _verb(parts, "is", "are")
    return KeyFinding(
        "unseen",
        "Some alerts reach none of your dashboards",
        f"{_joined(parts)} {verb} outside every panel's narrowing.",
        "Widen a panel to include them, or confirm they are meant to stay out of view.",
        None,
    )


def _is_critical(alert: AlertRow) -> bool:
    return (alert.severity or "").lower() == "critical"


def _is_ready(alert: AlertRow) -> bool:
    gaps = alert.readiness_rule_ids
    if "R8" in gaps or "R10" in gaps:
        return False
    return not (_is_critical(alert) and "R9" in gaps)


def _readiness(inputs: SummaryInputs) -> KeyFinding | None:
    v2 = [a for a in inputs.alerts if a.schema == "v2"]
    count = inputs.schemas["v2"].distinct_alerts if inputs.facts else len(v2)
    if not count:
        return None
    ready = sum(1 for a in v2 if _is_ready(a))
    critical = sum(1 for a in v2 if _is_critical(a) and "R9" in a.readiness_rule_ids)
    if inputs.facts is not None:
        ready = inputs.facts["schemas"]["v2"]["ready"]
        critical = inputs.facts["schemas"]["v2"]["critical"]
    if critical == 0:
        body = "Every critical alert has a runbook."
    elif critical == 1:
        body = "1 critical alert has no runbook."
    else:
        body = f"{critical:,} critical alerts have no runbook."
    return KeyFinding(
        "readiness",
        f"{ready:,} of {count:,} v2 alerts are phase-2 ready",
        body,
        "Add impact and an https:// runbook, starting with critical alerts.",
        None,
    )


def key_findings(inputs: SummaryInputs) -> tuple[KeyFinding, ...]:
    candidates = (
        _largest(inputs),
        _unassessed(inputs),
        _concentration(inputs),
        _hidden(inputs),
        _unseen(inputs),
        _readiness(inputs),
    )
    return tuple(f for f in candidates if f is not None)[:_MAX_FINDINGS]
