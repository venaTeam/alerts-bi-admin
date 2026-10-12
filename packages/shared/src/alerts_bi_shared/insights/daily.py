"""Pure conversion of stored UTC day buckets for the summary charts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from alerts_bi_shared.insights.model import DailyPoint


def daily_points(rows: Sequence[Mapping[str, Any]]) -> tuple[DailyPoint, ...]:
    """Day buckets as the slides' charts read them, ordered by schema then day."""
    points = [
        DailyPoint(
            alert_schema=str(row["alert_schema"]),
            day=row["snapshot_date"],
            covered_hours=float(row["covered_hours"]),
            distinct_alerts=int(row["distinct_alerts"] or 0),
            rule_flagged_distinct=int(row["flagged_by_rule_distinct"] or 0),
        )
        for row in rows
    ]
    return tuple(sorted(points, key=lambda p: (p.alert_schema, p.day)))
