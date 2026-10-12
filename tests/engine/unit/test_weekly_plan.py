"""Which weeks the schedule runs (design section 7.11), without a database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from src.engine.weekly.plan import WEEK, is_week_boundary, latest_week_end, plan_weeks

MONDAY = datetime(2026, 8, 24, tzinfo=UTC)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 8, 24, 0, 0, tzinfo=UTC), MONDAY),
        (datetime(2026, 8, 24, 0, 0, 1, tzinfo=UTC), MONDAY),
        (datetime(2026, 8, 25, 18, 0, tzinfo=UTC), MONDAY),
        (datetime(2026, 8, 30, 23, 59, tzinfo=UTC), MONDAY),
        (datetime(2026, 8, 31, 0, 0, tzinfo=UTC), MONDAY + WEEK),
        (datetime(2026, 8, 23, 23, 59, 59, tzinfo=UTC), MONDAY - WEEK),
    ],
)
def test_the_latest_week_ends_on_the_last_monday_boundary(
    now: datetime, expected: datetime
) -> None:
    assert latest_week_end(now) == expected


def test_a_naive_instant_is_read_as_utc() -> None:
    assert latest_week_end(datetime(2026, 8, 25, 18, 0)) == MONDAY


def test_the_boundary_is_monday_midnight_utc_only() -> None:
    assert is_week_boundary(MONDAY)
    assert is_week_boundary(MONDAY.replace(tzinfo=None))
    assert not is_week_boundary(MONDAY + timedelta(hours=18))
    assert not is_week_boundary(MONDAY + timedelta(days=1))


def test_a_new_team_gets_only_the_latest_completed_week() -> None:
    plan = plan_weeks(None, datetime(2026, 8, 25, 18, 0, tzinfo=UTC))
    assert plan.due == (MONDAY,) and not plan.expired and plan.blocked is None


def test_nothing_is_due_when_the_latest_week_is_published() -> None:
    assert plan_weeks(MONDAY, MONDAY + timedelta(days=6)).due == ()


def test_missed_weeks_are_caught_up_oldest_first() -> None:
    plan = plan_weeks(MONDAY, MONDAY + 3 * WEEK + timedelta(hours=2))
    assert plan.due == (MONDAY + WEEK, MONDAY + 2 * WEEK, MONDAY + 3 * WEEK)


def test_weeks_past_retention_are_expired_not_run() -> None:
    now = MONDAY + 20 * WEEK + timedelta(hours=1)
    plan = plan_weeks(MONDAY, now)
    assert len(plan.expired) + len(plan.due) == 20
    assert all(end - WEEK >= now - timedelta(days=84) for end in plan.due)
    assert all(end - WEEK < now - timedelta(days=84) for end in plan.expired)
    assert plan.expired[-1] + WEEK == plan.due[0]


def test_history_not_on_a_monday_boundary_blocks_the_team() -> None:
    plan = plan_weeks(datetime(2026, 8, 25, 18, 0), MONDAY + 2 * WEEK)
    assert plan.due == () and plan.blocked and "Monday" in plan.blocked
