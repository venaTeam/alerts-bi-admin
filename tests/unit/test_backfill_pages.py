"""Week selection and multi-week rendering without SQL (docs/backfill.md, 2026-10-19 redesign)."""

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


def options(html: str) -> list[tuple[str, bool, str]]:
    return [
        (value, checked == " checked", note)
        for value, checked, note in re.findall(
            r'<input type="checkbox" name="week" value="([\d-]+)"( checked)?>'
            r'<span class="abi-week-name">[^<]*</span><span class="abi-week-note[^"]*">([^<]*)<',
            html,
        )
    ]


def test_new_runs_lists_only_runnable_weeks_newest_first_with_the_newest_checked() -> None:
    html = pages.new_runs_dialog("checkout-api", "token", "Checkout API", calendar(), NOW)
    assert 'id="new-runs"' in html and 'action="/teams/checkout-api/trigger"' in html
    assert 'name="weeks_form" value="1"' in html and 'name="submission_key"' in html
    listed = options(html)
    # Published (10-12, 10-05), active (08-24), expired and incomplete weeks are not offered.
    assert [value for value, _, _ in listed] == [
        "2026-09-28",
        "2026-09-21",
        "2026-09-14",
        "2026-09-07",
        "2026-08-31",
        "2026-08-17",
        "2026-08-10",
        "2026-08-03",
    ]
    assert [value for value, checked, _ in listed if checked] == ["2026-09-28"]
    notes = {value: note for value, _, note in listed}
    assert notes["2026-09-07"] == "Unpublished run · runs again"
    assert notes["2026-09-28"] == "No run"
    assert notes["2026-08-03"] == "No run · leaves retention 19 Oct"
    assert 'class="abi-week-note warn">No run · leaves retention 19 Oct' in html
    assert '<option value="live" selected>' in html
    assert ">Start 1 run</button>" in html and "1 of 8 selected" in html
    assert "data-backfill-active" not in html


def test_new_runs_preselects_a_failed_week_and_warns_while_weeks_run() -> None:
    html = pages.new_runs_dialog(
        "checkout-api",
        "token",
        "Checkout API",
        calendar(),
        NOW,
        preselect=at("2026-09-07"),
        backfill_active=True,
    )
    assert [value for value, checked, _ in options(html) if checked] == ["2026-09-07"]
    assert "data-backfill-active" in html and "Several weeks are already running" in html


def test_new_runs_without_runnable_weeks_disables_starting() -> None:
    everything = {end.replace(tzinfo=None): "published" for end in pages.week_states({}, NOW)}
    html = pages.new_runs_dialog("checkout-api", "token", "Checkout API", everything, NOW)
    assert not options(html) and "is published, queued or running" in html
    assert '<button type="submit" class="abi-btn primary" disabled>Start runs</button>' in html
    assert pages.runnable_weeks(everything, NOW) == []


def test_retention_note_appears_only_for_a_gap_about_to_expire() -> None:
    note = pages.retention_note(calendar(), NOW, at("2026-07-27"))
    assert "27 Jul \u2013 3 Aug 2026" in note and "leaves Elasticsearch retention on 19 Oct" in note
    assert 'data-dialog="new-runs" data-week="2026-08-03"' in note
    everything = {end.replace(tzinfo=None): "completed" for end in pages.week_states({}, NOW)}
    assert pages.retention_note(everything, NOW, at("2026-07-27")) == ""
    # Weeks before a team's first run are not a gap, and a team without runs has none.
    assert pages.retention_note(calendar(), NOW, at("2026-08-03")) == ""
    assert pages.retention_note({}, NOW, None) == ""


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


def test_running_weeks_panel_polls_and_offers_cancel_and_remove() -> None:
    weeks = [
        week("a", "2026-08-03", "completed"),
        week("b", "2026-08-10", "running"),
        week("c", "2026-08-17", "queued"),
    ]
    html = pages.backfill_panel("checkout-api", BACKFILL, weeks, "token")
    assert "Running 3 weeks" in html and "1 of 3 completed · 1 running · 1 queued" in html
    assert f'data-backfill-url="/backfills/{"f" * 32}"' in html
    assert 'data-backfill-state="a:completed,b:running,c:queued"' in html
    assert f'action="/backfills/{"f" * 32}/cancel"' in html and "Cancel 1 queued week" in html
    assert 'action="/run-jobs/c/cancel"' in html and "Dismiss" not in html
    assert '<progress class="abi-bar" max="3" value="1"' in html
    assert "Model live" in html and "Open run" in html


def test_finished_weeks_panel_offers_retry_and_dismiss_without_polling() -> None:
    weeks = [
        week("a", "2026-08-03", "completed"),
        week("b", "2026-08-10", "failed", error_summary="Analysis failed.", attempts=2),
    ]
    html = pages.backfill_panel("checkout-api", BACKFILL, weeks, "token")
    assert "data-backfill-url" not in html and "Ran 2 weeks" in html
    assert "Finished with 1 failed" in html and "Retry failed week" in html
    assert 'action="/run-jobs/b/retry"' in html and "attempt 2" in html
    assert f'action="/backfills/{"f" * 32}/dismiss"' in html
