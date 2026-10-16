"""Backfill week selection and rendering without SQL (docs/backfill.md)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from src import pages
from src.engine.api.backfill import parse_weeks, selectable_weeks, week_rows

NOW = datetime(2026, 10, 14, 10, 42, tzinfo=UTC)


def at(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


def test_selectable_weeks_follow_the_84_day_retention() -> None:
    assert selectable_weeks(NOW) == (at("2026-08-03"), at("2026-10-12"))
    # A window start exactly 84 days old is still retained, one second later it is not.
    assert selectable_weeks(at("2026-10-19"))[0] == at("2026-08-03")
    assert selectable_weeks(datetime(2026, 10, 19, 0, 0, 1, tzinfo=UTC))[0] == at("2026-08-10")


def test_parse_weeks_sorts_distinct_mondays_and_refuses_other_days() -> None:
    assert parse_weeks(["2026-08-24", "2026-08-10", "2026-08-24"]) == [
        at("2026-08-10"),
        at("2026-08-24"),
    ]
    for value in ("2026-08-25", "24/08/2026", ""):
        with pytest.raises(HTTPException) as refused:
            parse_weeks([value])
        assert refused.value.status_code == 400


def test_week_rows_keep_the_latest_attempt_per_week() -> None:
    first = {"job_id": "a", "run_at": at("2026-08-10"), "status": "failed"}
    retry = {"job_id": "b", "run_at": at("2026-08-10"), "status": "queued"}
    older = {"job_id": "c", "run_at": at("2026-08-03"), "status": "completed"}
    rows = week_rows([older, first, retry])
    assert [(row["job_id"], row["attempts"]) for row in rows] == [("c", 1), ("b", 2)]


def calendar() -> dict[datetime, str]:
    naive = {
        "2026-10-12": "published",
        "2026-10-05": "published",
        "2026-09-07": "completed",
        "2026-08-24": "active",
    }
    return {datetime.fromisoformat(day): state for day, state in naive.items()}


def checkbox(html: str, day: str) -> str:
    match = re.search(
        rf'<label class="abi-cal-mon ([^"]*)"[^>]*><input ([^>]*value="{day}"[^>]*)>', html
    )
    assert match, day
    return match.group(1) + " | " + match.group(2)


def test_backfill_dialog_uses_sunday_first_calendars_with_selectable_mondays() -> None:
    html = pages.backfill_form("checkout-api", "token", "Checkout API", calendar(), NOW)
    assert 'id="backfill-weeks"' in html and 'action="/teams/checkout-api/backfill"' in html
    assert re.findall(r'class="abi-cal-name">([^<]+)<', html) == [
        "August 2026",
        "September 2026",
        "October 2026",
    ]
    grid = html.split('class="abi-cal-grid">', 1)[1]
    headers = re.findall(r'class="abi-cal-dow( mon)?" aria-hidden="true">(\w+)<', grid)[:7]
    assert [name for _, name in headers] == ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"]
    assert headers[1][0] == " mon"
    # August 1 2026 is a Saturday, so the first Sunday-first row starts on July 26.
    assert re.search(
        r'abi-cal-dow" aria-hidden="true">Sa</span><span class="abi-cal-day out"[^>]*>26<', html
    )
    oldest = checkbox(html, "2026-08-03")
    assert (
        "is-missing is-expiring" in oldest and "data-missing" in oldest and "disabled" not in oldest
    )
    assert "Leaves retention on 19 Oct" in oldest
    replaced = checkbox(html, "2026-09-07")
    assert replaced.startswith("is-completed") and "data-missing" not in replaced
    assert "disabled" not in replaced and "Runs this week again" in replaced
    for day in ("2026-10-12", "2026-08-24", "2026-10-19", "2026-10-26"):
        assert "disabled" in checkbox(html, day), day
    assert checkbox(html, "2026-10-19").startswith("is-future")
    assert "Select all 7 without a run" in html
    assert "leaves Elasticsearch retention on 19 Oct, in 4 days" in html
    assert '<option value="live" selected>' in html
    assert "27 Jul to 12 Oct 2026 · 11 weeks" in html


def test_gap_banner_counts_retained_weeks_without_a_run() -> None:
    banner = pages.gap_banner(calendar(), NOW)
    assert "7 weeks inside Elasticsearch retention have no run." in banner
    assert "The oldest, 27 Jul" in banner and "3 Aug 2026, leaves retention on 19 Oct" in banner
    assert 'data-dialog="backfill-weeks"' in banner
    everything = {end.replace(tzinfo=None): "completed" for end in pages.week_states({}, NOW)}
    assert pages.gap_banner(everything, NOW) == ""


def week(job: str, day: str, status: str, **extra: object) -> dict[str, object]:
    return {
        "job_id": job,
        "run_id": job * 64,
        "run_at": at(day),
        "status": status,
        "started_at": None,
        "completed_at": None,
        "error_summary": None,
        "attempts": 1,
        **extra,
    }


BACKFILL = {
    "backfill_id": "f" * 32,
    "requested_by": "operator",
    "submitted_at": datetime(2026, 10, 14, 10, 42),
    "llm_mode": "live",
    "cancelled_at": None,
}


def test_running_backfill_panel_polls_and_offers_cancel_and_remove() -> None:
    weeks = [
        week("a", "2026-08-03", "completed"),
        week("b", "2026-08-10", "running"),
        week("c", "2026-08-17", "queued"),
    ]
    html = pages.backfill_panel("checkout-api", BACKFILL, weeks, "token")
    assert "Backfill · 3 weeks" in html and "1 of 3 completed · 1 running · 1 queued" in html
    assert f'data-backfill-url="/backfills/{"f" * 32}"' in html
    assert 'data-backfill-state="a:completed,b:running,c:queued"' in html
    assert f'action="/backfills/{"f" * 32}/cancel"' in html and "Cancel 1 queued week" in html
    assert 'action="/run-jobs/c/cancel"' in html and "Dismiss" not in html
    assert '<progress class="abi-bf-bar" max="3" value="1"' in html
    assert "Model live" in html and "Open run" in html


def test_finished_backfill_panel_offers_retry_and_dismiss_without_polling() -> None:
    weeks = [
        week("a", "2026-08-03", "completed"),
        week("b", "2026-08-10", "failed", error_summary="Analysis failed.", attempts=2),
    ]
    html = pages.backfill_panel("checkout-api", BACKFILL, weeks, "token")
    assert "data-backfill-url" not in html
    assert "Finished with 1 failed" in html and "Retry failed week" in html
    assert 'action="/run-jobs/b/retry"' in html and "attempt 2" in html
    assert f'action="/backfills/{"f" * 32}/dismiss"' in html
