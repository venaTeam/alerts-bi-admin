"""Per-application rows, fire-frequency rows and the biggest single source (spec section 4)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from alerts_bi_shared.catalogs import CORE_RULE_IDS, R6_API_MIN_SPAN, R6_API_RATE_WINDOW
from alerts_bi_shared.insights.model import AlertRow, AppRow, FireRow

_DAY = R6_API_RATE_WINDOW
_MIN_SPAN = R6_API_MIN_SPAN


def _rule_number(rule_id: str) -> tuple[int, str]:
    digits = rule_id[1:]
    return (int(digits) if digits.isdigit() else 10**6, rule_id)


def by_application(alerts: tuple[AlertRow, ...]) -> tuple[AppRow, ...]:
    groups: dict[tuple[str, str], list[AlertRow]] = defaultdict(list)
    for alert in alerts:
        groups[(alert.application, alert.schema)].append(alert)
    rows: list[AppRow] = []
    for (application, schema), members in groups.items():
        rule_flagged = [a for a in members if a.quality_state == "rule_flagged"]
        llm_flagged = [a for a in members if a.quality_state == "llm_flagged"]
        rules = {rid for a in members for rid in a.core_rule_ids}
        rows.append(
            AppRow(
                application=application,
                schema=schema,
                alerts=len(members),
                rule_flagged_alerts=len(rule_flagged),
                llm_flagged_alerts=len(llm_flagged),
                events=sum(a.row_count for a in members),
                rule_flagged_events=sum(a.row_count for a in rule_flagged),
                llm_flagged_events=sum(a.row_count for a in llm_flagged),
                rules=tuple(sorted(rules, key=_rule_number)),
            )
        )
    rows.sort(
        key=lambda r: (
            -(r.rule_flagged_events + r.llm_flagged_events),
            -r.events,
            r.application,
            r.schema,
        )
    )
    return tuple(rows)


def fire_rows(alerts: tuple[AlertRow, ...], window_end: datetime) -> tuple[FireRow, ...]:
    """One row per alert. ``window_end`` is kept for callers; nothing here reads it any
    more, because ``open_hours`` ends at the last firing row, not at the week's end."""
    rows: list[FireRow] = []
    for alert in alerts:
        span = alert.last_seen - alert.first_seen
        rows.append(
            FireRow(
                alert=alert,
                span_hours=span.total_seconds() / 3600,
                max_episode_firing_rows=alert.max_episode_firing_rows,
                # When an episode is open the last row is firing, so last_seen is the open
                # episode's last firing row: this is how long its firing rows span.
                open_hours=(
                    None
                    if alert.open_since is None
                    else (alert.last_seen - alert.open_since).total_seconds() / 3600
                ),
                events_per_24h=(alert.row_count * (_DAY / span) if span >= _MIN_SPAN else None),
                pattern=alert.fire_pattern,
            )
        )
    rows.sort(
        key=lambda r: (
            -r.alert.row_count,
            r.alert.schema,
            r.alert.application,
            r.alert.key_field,
        )
    )
    return tuple(rows)


def biggest(alerts: tuple[AlertRow, ...]) -> AlertRow | None:
    if not alerts:
        return None
    return min(alerts, key=lambda a: (-a.row_count, a.schema, a.key_field))


def primary_rule_counts(alerts: tuple[AlertRow, ...], schema: str) -> tuple[tuple[str, int], ...]:
    """One schema's rule-flagged alerts partitioned by their primary rule.

    The primary rule is an alert's first core rule in catalogue order, so each alert counts
    once and the counts sum to the schema's rule-flagged alerts. ``(rule_id, alerts)`` pairs
    in catalogue order, zeros omitted. One schema only: v1 and v2 are never combined.
    """
    counts = dict.fromkeys(CORE_RULE_IDS, 0)
    for alert in alerts:
        if alert.schema != schema or alert.quality_state != "rule_flagged":
            continue
        primary = next((rid for rid in CORE_RULE_IDS if rid in alert.core_rule_ids), None)
        if primary is not None:
            counts[primary] += 1
    return tuple((rid, n) for rid, n in counts.items() if n)
