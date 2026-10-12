from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from alerts_bi_shared.window import build_run_window
from src.engine.domain.metrics import (
    DailyVolume,
    compute_daily_volume,
    ratio_or_none,
    rollup_volume,
)

from tests.engine.helpers.rows import v1_row, v2_row


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


WINDOW = build_run_window(at("2026-08-25T18:00:00Z"))


def by_date(daily: list[DailyVolume], date: str) -> DailyVolume:
    return next(d for d in daily if d.snapshot_date == date)


def test_empty_input_still_emits_every_bucket_in_the_window() -> None:
    daily = compute_daily_volume([], WINDOW)
    assert len(daily) == 8
    assert all(d.alerts == 0 and d.distinct_alerts == 0 for d in daily)


def test_alerts_counts_rows_and_distinct_alerts_counts_identities() -> None:
    # One stuck alert re-firing: many rows, one distinct identity.
    rows = [
        v1_row(**{"@timestamp": f"2026-08-20T0{i}:00:00Z", "key_field": "stuck"}) for i in range(9)
    ]
    day = by_date(compute_daily_volume(rows, WINDOW), "2026-08-20")
    assert day.alerts == 9
    assert day.distinct_alerts == 1


def test_alerts_per_hour_uses_the_bucket_real_covered_hours_not_24() -> None:
    rows = [
        v1_row(**{"@timestamp": "2026-08-18T19:00:00Z", "key_field": "k1"}),
        v1_row(**{"@timestamp": "2026-08-18T20:00:00Z", "key_field": "k2"}),
        v1_row(**{"@timestamp": "2026-08-18T21:00:00Z", "key_field": "k3"}),
    ]
    day = by_date(compute_daily_volume(rows, WINDOW), "2026-08-18")
    assert day.covered_hours == 6
    assert day.alerts_per_hour == 0.5


def test_rows_are_bucketed_by_the_utc_date_of_the_timestamp() -> None:
    daily = compute_daily_volume(
        [
            v1_row(**{"@timestamp": "2026-08-20T23:59:59Z", "key_field": "a"}),
            v1_row(**{"@timestamp": "2026-08-21T00:00:00Z", "key_field": "b"}),
        ],
        WINDOW,
    )
    assert by_date(daily, "2026-08-20").alerts == 1
    assert by_date(daily, "2026-08-21").alerts == 1


def test_a_row_outside_the_window_is_a_programming_error_not_a_silent_drop() -> None:
    with pytest.raises(ValueError, match="outside the run window"):
        compute_daily_volume([v1_row(**{"@timestamp": "2026-08-01T00:00:00Z"})], WINDOW)


def test_node_name_ratio_uses_only_nonempty_node_rows_on_both_sides() -> None:
    rows = [
        # eligible: one scope, two node names
        v1_row(
            **{
                "@timestamp": "2026-08-20T01:00:00Z",
                "application": "a",
                "object": "c",
                "node_name": "n1",
                "key_field": "k1",
            }
        ),
        v1_row(
            **{
                "@timestamp": "2026-08-20T02:00:00Z",
                "application": "a",
                "object": "c",
                "node_name": "n2",
                "key_field": "k2",
            }
        ),
        # ineligible: a different scope contributing no node name at all
        v1_row(
            **{
                "@timestamp": "2026-08-20T03:00:00Z",
                "application": "a",
                "object": "other",
                "node_name": None,
                "key_field": "k3",
            }
        ),
        v1_row(
            **{
                "@timestamp": "2026-08-20T04:00:00Z",
                "application": "a",
                "object": "other",
                "node_name": "   ",
                "key_field": "k4",
            }
        ),
    ]
    day = by_date(compute_daily_volume(rows, WINDOW), "2026-08-20")
    assert day.node_name_numerator == 2
    # 1, not 2: the node-less scope must not inflate the denominator.
    assert day.node_name_denominator == 1
    assert day.node_name_ratio == 2


def test_key_inflation_uses_all_rows_on_both_sides() -> None:
    rows = [
        v1_row(
            **{
                "@timestamp": f"2026-08-20T0{i}:00:00Z",
                "application": "a",
                "object": "c",
                "key_field": f"k{i}",
                "node_name": None,
            }
        )
        for i in range(1, 4)
    ]
    day = by_date(compute_daily_volume(rows, WINDOW), "2026-08-20")
    assert day.key_inflation_numerator == 3
    assert day.key_inflation_denominator == 1
    assert day.key_inflation_ratio == 3


def test_a_zero_denominator_produces_none_never_zero() -> None:
    day = by_date(compute_daily_volume([], WINDOW), "2026-08-20")
    assert day.node_name_ratio is None
    assert day.key_inflation_ratio is None
    assert ratio_or_none(0, 0) is None
    assert ratio_or_none(0, 5) == 0


def test_scorecard_alerts_per_hour_divides_by_the_full_168_hours() -> None:
    start = at("2026-08-19T00:00:00Z")
    rows = [
        v1_row(
            **{
                "@timestamp": (start + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"),
                "key_field": f"k{i}",
            }
        )
        for i in range(168)
    ]
    rollup = rollup_volume(compute_daily_volume(rows, WINDOW))
    assert rollup.alerts == 168
    assert rollup.alerts_per_hour == 1


def test_published_distinct_is_sum_daily_distinct_over_seven() -> None:
    # One identity present on two dates: daily inventory is 1 on each, so 2/7.
    rows = [
        v1_row(**{"@timestamp": "2026-08-20T10:00:00Z", "key_field": "same"}),
        v1_row(**{"@timestamp": "2026-08-21T10:00:00Z", "key_field": "same"}),
    ]
    rollup = rollup_volume(compute_daily_volume(rows, WINDOW))
    assert rollup.distinct_alerts_per_day == pytest.approx(2 / 7)


def test_diagnostic_rollup_divides_summed_numerators_by_summed_denominators() -> None:
    rows = [
        v1_row(
            **{
                "@timestamp": "2026-08-20T01:00:00Z",
                "application": "a",
                "object": "c",
                "node_name": "n1",
                "key_field": "k1",
            }
        ),
        v1_row(
            **{
                "@timestamp": "2026-08-20T02:00:00Z",
                "application": "a",
                "object": "c",
                "node_name": "n2",
                "key_field": "k2",
            }
        ),
        v1_row(
            **{
                "@timestamp": "2026-08-21T01:00:00Z",
                "application": "a",
                "object": "c",
                "node_name": "n3",
                "key_field": "k3",
            }
        ),
    ]
    rollup = rollup_volume(compute_daily_volume(rows, WINDOW))
    # day 20: 2/1, day 21: 1/1 -> summed 3/2 = 1.5, not the mean of 2 and 1.
    assert rollup.node_name_numerator == 3
    assert rollup.node_name_denominator == 2
    assert rollup.node_name_ratio == 1.5


def test_v1_and_v2_are_computed_independently_and_never_merged_here() -> None:
    v1 = rollup_volume(
        compute_daily_volume([v1_row(**{"@timestamp": "2026-08-20T01:00:00Z"})], WINDOW)
    )
    v2 = rollup_volume(
        compute_daily_volume([v2_row(**{"@timestamp": "2026-08-20T01:00:00Z"})], WINDOW)
    )
    assert v1.alerts == 1
    assert v2.alerts == 1
