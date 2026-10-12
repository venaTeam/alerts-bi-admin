"""Run window and UTC daily bucketing (design section 6; flow steps 1 and 3).

``run_at`` is captured once. The window is the exact half-open UTC range
``[run_at - 168h, run_at)``. A rolling 168-hour range normally touches eight UTC calendar
dates, so the first and last buckets are usually partial; each bucket therefore carries its
own real boundaries and ``covered_hours`` rather than being assumed to be a full day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

__all__ = [
    "WINDOW_DAYS",
    "WINDOW_HOURS",
    "DailyBucket",
    "RunWindow",
    "build_run_window",
    "utc_date_key",
]

WINDOW_DAYS = 7
WINDOW_HOURS = 168

_DAY = timedelta(days=1)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class DailyBucket:
    snapshot_date: str
    """UTC calendar date, ``YYYY-MM-DD``."""
    bucket_start: datetime
    """Inclusive."""
    bucket_end: datetime
    """Exclusive."""
    covered_hours: float
    """Real hours of this date inside the window."""


@dataclass(frozen=True, slots=True)
class RunWindow:
    run_at: datetime
    window_start: datetime
    """Inclusive."""
    window_end: datetime
    """Exclusive; equals ``run_at``."""
    buckets: tuple[DailyBucket, ...]
    """Ascending by date."""

    @property
    def snapshot_dates(self) -> list[str]:
        return [bucket.snapshot_date for bucket in self.buckets]

    def contains(self, at: datetime) -> bool:
        """Is an instant inside the half-open window?"""
        return self.window_start <= _as_utc(at) < self.window_end


def _as_utc(value: datetime) -> datetime:
    """Interpret a naive datetime as UTC; convert an aware one."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def utc_date_key(at: datetime) -> str:
    """UTC calendar date of an instant, as ``YYYY-MM-DD``."""
    return _as_utc(at).strftime("%Y-%m-%d")


def _utc_midnight(at: datetime) -> datetime:
    """Midnight UTC starting the calendar date containing ``at``."""
    aware = _as_utc(at)
    return datetime(aware.year, aware.month, aware.day, tzinfo=UTC)


def build_run_window(run_at: datetime) -> RunWindow:
    """Build the run window and its daily buckets."""
    end = _as_utc(run_at)
    start = end - timedelta(hours=WINDOW_HOURS)

    buckets: list[DailyBucket] = []
    # Walk calendar dates from the one containing window_start up to the one containing
    # the last instant inside the window. window_end is exclusive, so a run at exactly
    # midnight must not open an empty bucket for the date it stops at.
    last_instant = end - timedelta(microseconds=1)
    day = _utc_midnight(start)
    final_day = _utc_midnight(last_instant)
    while day <= final_day:
        bucket_start = max(start, day)
        bucket_end = min(end, day + _DAY)
        if bucket_end > bucket_start:
            buckets.append(
                DailyBucket(
                    snapshot_date=utc_date_key(day),
                    bucket_start=bucket_start,
                    bucket_end=bucket_end,
                    covered_hours=(bucket_end - bucket_start).total_seconds() / 3600,
                )
            )
        day += _DAY

    return RunWindow(
        run_at=end,
        window_start=start,
        window_end=end,
        buckets=tuple(buckets),
    )
