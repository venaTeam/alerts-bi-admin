"""Which weeks are due for a team (design section 7.11).

Every team's week runs from Monday 00:00 UTC to the next Monday 00:00 UTC, and ``run_at`` is
always that Monday boundary - never the moment the scheduler happened to start. A run that
starts hours late still covers exactly the right week, and every team's weeks line up.

The plan is a pure function of the team's latest published week and the current time:

* a team with nothing published gets the most recent completed week only;
* a team with history gets every completed week after its latest published one, oldest first,
  so a missed scheduler run is caught up rather than left as a gap;
* a week that is already older than Elasticsearch retention is reported as expired instead of
  being run - its data is gone, and publishing zeros would claim a quiet week that never was;
* a team whose history does not end on a Monday boundary (published by hand before the
  schedule existed) is blocked for an operator, because no Monday week can be adjacent to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from alerts_bi_shared.window import WINDOW_HOURS

__all__ = [
    "RETENTION_DAYS",
    "WEEK",
    "WeeklyPlan",
    "is_week_boundary",
    "latest_week_end",
    "plan_weeks",
]

WEEK = timedelta(hours=WINDOW_HOURS)

#: How far back a week may start and still be run. Elasticsearch keeps three months; twelve
#: weeks stays clear of the edge, so a week is never run over partly expired data.
RETENTION_DAYS = 84


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def is_week_boundary(moment: datetime) -> bool:
    """Is this instant a Monday 00:00:00 UTC?"""
    at = _utc(moment)
    return at.weekday() == 0 and (at.hour, at.minute, at.second, at.microsecond) == (0, 0, 0, 0)


def latest_week_end(now: datetime) -> datetime:
    """The end of the most recent week that has completely ended by ``now``."""
    at = _utc(now)
    midnight = at.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=midnight.weekday())


@dataclass(frozen=True, slots=True)
class WeeklyPlan:
    #: Week ends to run and publish, oldest first.
    due: tuple[datetime, ...] = ()
    #: Week ends skipped because their data is past retention, oldest first.
    expired: tuple[datetime, ...] = ()
    #: Why nothing can be scheduled for this team until an operator acts, if so.
    blocked: str | None = None


def plan_weeks(
    latest_published_end: datetime | None,
    now: datetime,
    *,
    retention_days: int = RETENTION_DAYS,
) -> WeeklyPlan:
    """The weeks due for one team at ``now``."""
    last = latest_week_end(now)
    if latest_published_end is None:
        return WeeklyPlan(due=(last,))

    latest = _utc(latest_published_end)
    if not is_week_boundary(latest):
        return WeeklyPlan(
            blocked=(
                f"the latest published week ends {latest:%Y-%m-%d %H:%M} UTC, not on a Monday "
                "00:00 UTC boundary, so no scheduled week can follow it; publish the first "
                "Monday-aligned week by hand with --allow-gap, or withdraw the unaligned weeks"
            )
        )

    ends: list[datetime] = []
    end = latest + WEEK
    while end <= last:
        ends.append(end)
        end += WEEK

    earliest_start = _utc(now) - timedelta(days=retention_days)
    expired = tuple(end for end in ends if end - WEEK < earliest_start)
    due = tuple(end for end in ends if end - WEEK >= earliest_start)
    return WeeklyPlan(due=due, expired=expired)
