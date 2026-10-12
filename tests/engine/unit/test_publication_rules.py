"""The publication rules (design section 7.10), without a database.

Published weeks are back to back: never overlapping, and not leaving a gap unless the
operator says so. These are the rules that keep the portal's history honest, so every
branch is pinned here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from alerts_bi_operations.review.publication import (
    PublicationRefused,
    PublishedWeek,
    check_publication,
)

WEEK = timedelta(hours=168)
END = datetime(2026, 8, 30, 16, 44, 35, tzinfo=UTC)


def week(end: datetime, run_id: str) -> PublishedWeek:
    return PublishedWeek(run_id=run_id, window_start=end - WEEK, window_end=end)


def naive(value: PublishedWeek) -> PublishedWeek:
    """SQL Server hands back naive UTC datetimes; the rules must compare them correctly."""
    return PublishedWeek(
        value.run_id,
        value.window_start.replace(tzinfo=None),
        value.window_end.replace(tzinfo=None),
        value.publication_id,
    )


def test_the_first_publication_of_a_team_is_accepted() -> None:
    assert check_publication(week(END, "new"), [], replace=False, allow_gap=False) is None


def test_the_week_directly_after_the_latest_is_accepted() -> None:
    current = [week(END - WEEK, "previous")]
    assert check_publication(week(END, "new"), current, replace=False, allow_gap=False) is None


def test_naive_database_datetimes_compare_as_utc() -> None:
    current = [naive(week(END - WEEK, "previous"))]
    assert check_publication(week(END, "new"), current, replace=False, allow_gap=False) is None


@pytest.mark.parametrize("shift_hours", [1, 49, 167])
def test_an_overlapping_week_is_always_refused(shift_hours: int) -> None:
    current = [week(END, "published")]
    candidate = week(END + timedelta(hours=shift_hours), "overlapping")
    with pytest.raises(PublicationRefused, match="overlaps"):
        check_publication(candidate, current, replace=False, allow_gap=True)


def test_a_gap_after_the_latest_week_is_refused_without_allow_gap() -> None:
    current = [week(END - 2 * WEEK, "old")]
    with pytest.raises(PublicationRefused, match="gap"):
        check_publication(week(END, "new"), current, replace=False, allow_gap=False)


def test_a_gap_is_accepted_when_the_operator_allows_it() -> None:
    current = [week(END - 2 * WEEK, "old")]
    assert check_publication(week(END, "new"), current, replace=False, allow_gap=True) is None


def test_filling_the_missing_week_between_two_published_weeks_is_accepted() -> None:
    current = [week(END - WEEK, "before"), week(END + WEEK, "after")]
    assert check_publication(week(END, "fill"), current, replace=False, allow_gap=False) is None


def test_a_week_before_the_history_that_leaves_a_gap_is_refused() -> None:
    current = [week(END + 2 * WEEK, "later")]
    with pytest.raises(PublicationRefused, match="gap"):
        check_publication(week(END, "earlier"), current, replace=False, allow_gap=False)


def test_the_same_week_needs_replace() -> None:
    current = [week(END, "published")]
    with pytest.raises(PublicationRefused, match="--replace"):
        check_publication(week(END, "rerun"), current, replace=False, allow_gap=False)


def test_replace_returns_the_publication_it_withdraws() -> None:
    published = week(END, "published")
    replaced = check_publication(
        week(END, "rerun"), [week(END - WEEK, "before"), published], replace=True, allow_gap=False
    )
    assert replaced == published


def test_replacing_a_week_does_not_ask_again_about_a_gap_already_accepted() -> None:
    published = week(END, "published")
    current = [week(END - 3 * WEEK, "old"), published]
    assert (
        check_publication(week(END, "rerun"), current, replace=True, allow_gap=False) == published
    )


def test_replace_without_a_matching_week_is_refused() -> None:
    with pytest.raises(PublicationRefused, match="no published week"):
        check_publication(
            week(END, "new"), [week(END - WEEK, "prev")], replace=True, allow_gap=False
        )


def test_publishing_the_same_run_twice_is_refused() -> None:
    with pytest.raises(PublicationRefused, match="already published"):
        check_publication(week(END, "same"), [week(END, "same")], replace=True, allow_gap=True)


def test_a_window_that_is_not_exactly_168_hours_is_refused() -> None:
    odd = PublishedWeek("odd", END - timedelta(hours=100), END)
    with pytest.raises(PublicationRefused, match="168"):
        check_publication(odd, [], replace=False, allow_gap=True)
