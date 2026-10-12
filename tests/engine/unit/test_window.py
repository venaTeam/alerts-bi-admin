from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from alerts_bi_shared.window import WINDOW_HOURS, build_run_window, utc_date_key


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def test_window_is_the_exact_half_open_168_hour_utc_range_ending_at_run_at() -> None:
    w = build_run_window(at("2026-08-25T18:00:00Z"))
    assert w.window_end == at("2026-08-25T18:00:00Z")
    assert w.window_start == at("2026-08-18T18:00:00Z")
    assert (w.window_end - w.window_start) == timedelta(hours=WINDOW_HOURS)


def test_a_mid_day_run_touches_eight_utc_dates_with_partial_first_and_last_buckets() -> None:
    w = build_run_window(at("2026-08-25T18:00:00Z"))
    assert len(w.buckets) == 8
    assert w.buckets[0].snapshot_date == "2026-08-18"
    assert w.buckets[0].covered_hours == 6
    assert w.buckets[7].snapshot_date == "2026-08-25"
    assert w.buckets[7].covered_hours == 18
    for bucket in w.buckets[1:7]:
        assert bucket.covered_hours == 24


@pytest.mark.parametrize(
    "run_at",
    [
        "2026-08-25T18:00:00Z",
        "2026-08-25T00:00:00Z",
        "2026-08-25T23:59:59.999Z",
        "2026-01-01T07:13:44.512Z",
        "2026-03-01T12:00:00Z",
    ],
)
def test_covered_hours_always_sum_to_exactly_168(run_at: str) -> None:
    w = build_run_window(at(run_at))
    assert sum(b.covered_hours for b in w.buckets) == pytest.approx(WINDOW_HOURS)


def test_a_run_at_exactly_midnight_yields_seven_whole_buckets_and_no_empty_eighth() -> None:
    w = build_run_window(at("2026-08-25T00:00:00Z"))
    assert len(w.buckets) == 7
    assert w.buckets[0].snapshot_date == "2026-08-18"
    assert w.buckets[6].snapshot_date == "2026-08-24"
    for bucket in w.buckets:
        assert bucket.covered_hours == 24


def test_buckets_are_contiguous_ascending_and_non_overlapping() -> None:
    w = build_run_window(at("2026-08-25T18:00:00Z"))
    assert w.buckets[0].bucket_start == w.window_start
    assert w.buckets[-1].bucket_end == w.window_end
    for previous, current in zip(w.buckets[:-1], w.buckets[1:], strict=True):
        assert current.bucket_start == previous.bucket_end


def test_the_window_is_inclusive_at_the_start_and_exclusive_at_the_end() -> None:
    w = build_run_window(at("2026-08-25T18:00:00Z"))
    assert w.contains(w.window_start) is True
    assert w.contains(w.window_start - timedelta(microseconds=1)) is False
    assert w.contains(w.window_end - timedelta(microseconds=1)) is True
    assert w.contains(w.window_end) is False


def test_a_window_crossing_a_month_boundary_buckets_by_real_utc_dates() -> None:
    w = build_run_window(at("2026-03-03T06:00:00Z"))
    assert w.snapshot_dates == [
        "2026-02-24",
        "2026-02-25",
        "2026-02-26",
        "2026-02-27",
        "2026-02-28",
        "2026-03-01",
        "2026-03-02",
        "2026-03-03",
    ]
    assert w.buckets[0].covered_hours == 18
    assert w.buckets[7].covered_hours == 6


def test_utc_date_key_uses_utc_not_local_time() -> None:
    assert utc_date_key(at("2026-08-25T23:59:59.999Z")) == "2026-08-25"
    assert utc_date_key(at("2026-08-26T00:00:00Z")) == "2026-08-26"


def test_a_naive_datetime_is_interpreted_as_utc() -> None:
    naive = build_run_window(datetime(2026, 8, 25, 18, 0, 0))
    aware = build_run_window(datetime(2026, 8, 25, 18, 0, 0, tzinfo=UTC))
    assert naive.window_start == aware.window_start
    assert naive.snapshot_dates == aware.snapshot_dates
