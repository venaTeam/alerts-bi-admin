"""Volume and diagnostic metrics (design sections 3.3 and 3.7; flow step 3).

Two counts always travel together: ``alerts`` (raw rows - pipeline load) and
``distinct_alerts`` (distinct ``application + key_field`` - how many things actually
fired). One stuck v1 alert is 288 rows a day and one distinct alert, and a team needs the
first number to care and the second to act.

Every distinct figure is published as a daily rate, never as a window total: a 7-day total
is 7x a 1-day total for arithmetic reasons alone.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from alerts_bi_shared.window import WINDOW_HOURS, RunWindow

from .normalize import AlertRecord

__all__ = [
    "WINDOW_DAYS",
    "DailyVolume",
    "VolumeRollup",
    "compute_daily_volume",
    "ratio_or_none",
    "rollup_volume",
]

#: Days in the reporting window; the divisor for the published distinct daily rate.
WINDOW_DAYS = 7


def _tuple_key(*parts: str | None) -> str:
    """Length-prefixed tuple key, so ('a','bc') and ('ab','c') never collide."""
    return "|".join(f"{len(p or '')}:{p or ''}" for p in parts)


def _has_node_name(row: AlertRecord) -> bool:
    """Does this row participate in the node diagnostic?

    Design section 3.7: only nonempty-node rows are used, on *both* sides of the ratio.
    Otherwise the denominator would silently include component scopes that could never
    contribute a node name.
    """
    return isinstance(row.node_name, str) and row.node_name.strip() != ""


def ratio_or_none(numerator: int, denominator: int) -> float | None:
    """A zero denominator yields ``None``, never zero.

    "No eligible rows" and "a ratio of zero" are different statements and must not be
    conflated in the report.
    """
    return None if denominator == 0 else numerator / denominator


@dataclass(frozen=True, slots=True)
class DailyVolume:
    snapshot_date: str
    bucket_start: datetime
    bucket_end: datetime
    covered_hours: float
    alerts: int
    distinct_alerts: int
    alerts_per_hour: float
    node_name_numerator: int
    node_name_denominator: int
    node_name_ratio: float | None
    key_inflation_numerator: int
    key_inflation_denominator: int
    key_inflation_ratio: float | None


@dataclass(frozen=True, slots=True)
class VolumeRollup:
    alerts: int
    """Sum of bucket row counts."""
    alerts_per_hour: float
    """Rows divided by the full 168 hours."""
    distinct_alerts_per_day: float
    """``sum(daily distinct) / 7``."""
    node_name_numerator: int
    node_name_denominator: int
    node_name_ratio: float | None
    key_inflation_numerator: int
    key_inflation_denominator: int
    key_inflation_ratio: float | None


def compute_daily_volume(rows: Sequence[AlertRecord], window: RunWindow) -> list[DailyVolume]:
    """Compute one schema's daily volume rows over the run window.

    Buckets with no rows are still emitted: a day a team fired nothing is a real
    observation about that week, and dropping it would make the daily breakdown depend on
    activity.
    """
    by_date: dict[str, list[AlertRecord]] = {b.snapshot_date: [] for b in window.buckets}
    for row in rows:
        rows_for_date = by_date.get(row.snapshot_date)
        if rows_for_date is None:
            raise ValueError(
                f"row at {row.timestamp.isoformat()} falls outside the run window buckets"
            )
        rows_for_date.append(row)

    daily: list[DailyVolume] = []
    for bucket in window.buckets:
        day_rows = by_date[bucket.snapshot_date]

        identities: set[str] = set()
        all_scopes: set[str] = set()
        node_tuples: set[str] = set()
        node_scopes: set[str] = set()

        for row in day_rows:
            identities.add(row.identity)
            scope = _tuple_key(row.application, row.component)
            all_scopes.add(scope)
            if _has_node_name(row):
                node_tuples.add(_tuple_key(row.application, row.component, row.node_name))
                node_scopes.add(scope)

        alerts = len(day_rows)
        node_numerator = len(node_tuples)
        node_denominator = len(node_scopes)
        key_numerator = len(identities)
        key_denominator = len(all_scopes)

        daily.append(
            DailyVolume(
                snapshot_date=bucket.snapshot_date,
                bucket_start=bucket.bucket_start,
                bucket_end=bucket.bucket_end,
                covered_hours=bucket.covered_hours,
                alerts=alerts,
                distinct_alerts=len(identities),
                alerts_per_hour=alerts / bucket.covered_hours,
                node_name_numerator=node_numerator,
                node_name_denominator=node_denominator,
                node_name_ratio=ratio_or_none(node_numerator, node_denominator),
                key_inflation_numerator=key_numerator,
                key_inflation_denominator=key_denominator,
                key_inflation_ratio=ratio_or_none(key_numerator, key_denominator),
            )
        )
    return daily


def rollup_volume(daily: Sequence[DailyVolume]) -> VolumeRollup:
    """Roll daily rows up to the 168-hour scorecard figures.

    ``distinct_alerts_per_day`` is ``sum(daily distinct) / 7`` and NOT the distinct
    identity count across the whole window: this measures average daily inventory, so an
    identity present on two dates contributes once to each. The complete-window distinct
    count is still used internally for deduplication, but it is not the published headline.

    Diagnostics divide summed numerators by summed denominators rather than averaging
    already-rounded daily ratios.
    """
    alerts = sum(d.alerts for d in daily)
    distinct_sum = sum(d.distinct_alerts for d in daily)
    node_numerator = sum(d.node_name_numerator for d in daily)
    node_denominator = sum(d.node_name_denominator for d in daily)
    key_numerator = sum(d.key_inflation_numerator for d in daily)
    key_denominator = sum(d.key_inflation_denominator for d in daily)

    return VolumeRollup(
        alerts=alerts,
        alerts_per_hour=alerts / WINDOW_HOURS,
        distinct_alerts_per_day=distinct_sum / WINDOW_DAYS,
        node_name_numerator=node_numerator,
        node_name_denominator=node_denominator,
        node_name_ratio=ratio_or_none(node_numerator, node_denominator),
        key_inflation_numerator=key_numerator,
        key_inflation_denominator=key_denominator,
        key_inflation_ratio=ratio_or_none(key_numerator, key_denominator),
    )
