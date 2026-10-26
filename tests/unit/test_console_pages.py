"""The redesigned console pages without SQL (design "Operator console redesign", 2026-10-19)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src import app as console
from src import pages
from src.app import build_admin
from src.auth import csrf_token
from src.config import AdminSettings, load_config
from src.engine.api.jobs import PUBLISHED_WHILE_WAITING

START, END = datetime(2026, 10, 5), datetime(2026, 10, 12)
RUN = "1" * 64


def run(**extra: Any) -> dict[str, Any]:
    return {
        "run_id": RUN,
        "team_id": "checkout-api",
        "team_display_name": "Checkout API",
        "run_at": END,
        "window_start": START,
        "window_end": END,
        "status": "completed",
        "started_at": datetime(2026, 10, 12, 0, 3),
        "completed_at": datetime(2026, 10, 12, 0, 41),
        "error_summary": None,
        "publication_id": None,
        "published_by": None,
        "published_at": None,
        "review_note": None,
        "withdrawn": 0,
        "schedule_outcome": None,
        "unassessed": 0,
        "findings_count": 3,
        **extra,
    }


def card(**extra: Any) -> pages.TeamCard:
    return pages.team_card("checkout-api", "Checkout API", True, {"run": run(**extra)})


def no_inline_styles(html: str) -> None:
    assert " style=" not in html
    assert re.findall(r"<script[^>]*>.*?</script>", html) == [
        f'<script src="{pages.SCRIPT_PATH}" defer></script>'
    ]


@pytest.mark.parametrize(
    ("extra", "steps", "status", "attention"),
    [
        ({}, ("done", "current", "todo"), "Ready to review · 5 Oct \u2013 12 Oct 2026", True),
        (
            {"schedule_outcome": "held", "unassessed": 14},
            ("done", "current", "todo"),
            "Held · 14 not assessed",
            True,
        ),
        (
            {"withdrawn": 1},
            ("done", "current", "todo"),
            "Withdrawn · 5 Oct \u2013 12 Oct 2026",
            True,
        ),
        (
            {"publication_id": 7},
            ("done", "done", "done"),
            "Published · 5 Oct \u2013 12 Oct 2026",
            False,
        ),
        (
            {"status": "failed"},
            ("fail", "todo", "todo"),
            "Run failed · 5 Oct \u2013 12 Oct 2026",
            True,
        ),
    ],
)
def test_team_cards_show_the_newest_week_as_steps(
    extra: dict[str, Any], steps: tuple[str, str, str], status: str, attention: bool
) -> None:
    shown = card(**extra)
    assert (shown.steps, shown.status, shown.attention) == (steps, status, attention)


def test_team_cards_show_active_submissions_and_teams_without_runs() -> None:
    running = {
        "run_at": datetime(2026, 10, 19),
        "status": "running",
        "started_at": datetime(2026, 10, 19, 2, 10),
    }
    older = {"run_at": datetime(2026, 9, 28), "status": "queued", "started_at": None}
    shown = pages.team_card(
        "a", "A", False, {"run": run(publication_id=1), "active": [older, running]}
    )
    assert shown.steps == ("current", "todo", "todo") and shown.tag == "Manual"
    assert shown.status == "Running since 19 Oct, 02:10 UTC · 1 more week in progress"
    assert not shown.attention
    empty = pages.team_card("b", "B", True, {})
    assert (empty.steps, empty.status, empty.attention) == (("todo",) * 3, "No runs yet", False)


def test_teams_page_puts_attention_first_and_filters_it() -> None:
    teams = [("calm", "Calm", True), ("broken", "Broken", True), ("new", "New", False)]
    overview = {"calm": {"run": run(publication_id=1)}, "broken": {"run": run(status="failed")}}
    html = pages.dashboard_page(teams, overview, None, False)
    no_inline_styles(html)
    names = re.findall(r'class="abi-card-name"><span>([^<]+)<', html)
    assert names == ["Broken", "Calm", "New"]
    assert "All · 3" in html and "Needs you · 1" in html and ">Manual<" in html
    assert 'href="/teams/broken?tab=overview"' in html
    attention = pages.dashboard_page(teams, overview, None, False, "attention")
    assert re.findall(r'class="abi-card-name"><span>([^<]+)<', attention) == ["Broken"]
    calm = pages.dashboard_page(teams[:1], {"calm": overview["calm"]}, None, False, "attention")
    assert "Nothing needs you right now" in calm


def test_layout_has_the_sidebar_and_no_top_bar() -> None:
    html = pages.layout("Teams", "<p>x</p>", "Saved.", nav="schedule")
    assert '<aside class="abi-side">' in html and "abi-top" not in html
    assert '<a class="abi-nav" href="/schedule" aria-current=page>' in html
    assert "Signed in" not in html and 'class="abi-flash"' in html
    assert pages.STYLESHEET_PATH in html


@pytest.mark.parametrize(
    ("selected", "job", "view", "expected"),
    [
        (run(), None, "review", ["done", "", "todo"]),
        (run(), None, "publish", ["done", "done", ""]),
        (run(publication_id=1, published_by="weekly-schedule"), None, "published", ["done"] * 3),
        (
            run(status="failed", error_summary="Source unavailable"),
            None,
            "failed",
            ["fail", "todo", "todo"],
        ),
        (None, {"status": "running", "started_at": END}, "job", ["running", "todo", "todo"]),
        (None, {"status": "pending"}, "job", ["", "todo", "todo"]),
    ],
)
def test_week_steps_follow_the_selected_run(
    selected: dict[str, Any] | None, job: dict[str, Any] | None, view: str, expected: list[str]
) -> None:
    html = pages.week_steps(selected, job, view)
    assert re.findall(r'<li class="abi-step ([a-z]*)"', html) == expected
    if selected and selected.get("published_by") == "weekly-schedule":
        assert "Checked automatically" in html and "Done · by the schedule" in html


def test_review_shows_held_and_withdrawn_notes_and_leads_to_publish() -> None:
    held = pages.review_panel(
        run(schedule_outcome="held", unassessed=14),
        "<totals>",
        "<findings>",
        2,
        withdrawal={"at": END, "note": "Wrong week"},
    )
    assert "The model couldn&#x27;t assess 14 alerts, so this week is held." in held
    assert "three days after it was first held" in held
    assert "Withdrawn on" in held and "Wrong week" in held and "Withdrawn by" not in held
    assert f"/teams/checkout-api?tab=publish&amp;run_id={RUN}" in held
    assert "2 alerts with findings" in held


def test_publish_step_offers_replace_only_when_another_run_is_published() -> None:
    plain = pages.publish_panel(run(), "csrf", alerts=312, flagged=41, unassessed=14, replace=False)
    assert f'action="/runs/{RUN}/publish"' in plain and 'name="note"' in plain
    assert 'name="replace"' not in plain and "<dd>312</dd>" in plain
    assert "The 14 alerts the model couldn't assess" in plain
    replacing = pages.publish_panel(run(), "csrf", alerts=1, flagged=0, unassessed=0, replace=True)
    assert '<input type="checkbox" name="replace" value="1" required>' in replacing
    assert "not assessed" in replacing and "couldn" not in replacing


ROW = {
    "alert_schema": "v2",
    "application": "payments-api",
    "key_field": "key-1",
    "message": "<b>latency</b>",
    "component": "gateway",
    "severity": "high",
    "environment": "prod",
    "row_count": 4210,
    "core_rule_ids": "R1",
    "readiness_rule_ids": "",
    "quality_state": "rule_flagged",
    "llm_principle_id": None,
    "llm_confidence": None,
    "llm_justification": None,
    "findings_evidence": "[]",
    "representative_doc": "{}",
    "first_seen": START,
    "last_seen": END,
}


def decision(state: str, run_id: str = RUN) -> dict[str, Any]:
    return {
        "finding_id": "R1",
        "state": state,
        "note": f"{state} note",
        "decided_by": "alice",
        "decided_at": END,
        "run_id": run_id,
    }


def test_findings_list_shows_this_runs_decision_only() -> None:
    identity = pages.identity(ROW)
    earlier = {identity: [decision("dismissed", "2" * 64)]}
    html = pages.findings_list(run(), [ROW], 1, 1, earlier, decisions=True)
    assert "No decision" in html and "Dismissed" not in html
    assert "tab=findings" in html and "finding=" in html
    current = pages.findings_list(
        run(), [ROW], 1, 1, {identity: [decision("confirmed")]}, decisions=True
    )
    assert ">Confirmed<" in current
    review = pages.findings_list(run(), [ROW], 1, 1, {}, decisions=False)
    assert "No decision" not in review and "4,210 events" in review


def test_finding_page_records_decisions_only_after_publication() -> None:
    unpublished = pages.finding_page("Checkout API", run(), ROW, "R1", [], "csrf", 1, None, False)
    no_inline_styles(unpublished)
    assert "Publish this run before recording a decision." in unpublished
    assert "/decide" not in unpublished and "&lt;b&gt;latency&lt;/b&gt;" in unpublished
    published = pages.finding_page(
        "Checkout API",
        run(publication_id=1),
        ROW,
        "R1",
        [decision("dismissed", "2" * 64), decision("confirmed")],
        "csrf",
        1,
        None,
        False,
    )
    assert (
        f'action="/runs/{RUN}/decide"' in published and 'data-dialog="record-decision"' in published
    )
    assert 'name="state" value="confirmed" checked' in published
    assert "alice" not in published, "who decided is not shown"
    assert (
        "confirmed note" in published
        and "Decision history" in published
        and "dismissed note" in published
    )


def test_decisions_page_keeps_filters_in_pagination() -> None:
    rows = [
        {
            **decision("pending"),
            "team_id": "checkout-api",
            "team_display_name": "Checkout API",
            "alert_schema": "v2",
            "application": "payments-api",
            "key_field": "key-1",
            "window_start": START,
            "window_end": END,
        }
    ]
    teams = [("checkout-api", "Checkout API", True)]
    html = pages.decisions_page(
        rows, 120, 2, teams, team="checkout-api", state="pending", counts=(7, 130)
    )
    no_inline_styles(html)
    assert (
        '<option value="checkout-api" selected>' in html
        and '<option value="pending" selected>' in html
    )
    assert "/decisions?team=checkout-api&amp;state=pending&amp;view=recorded&amp;page=1" in html
    assert "/decisions?team=checkout-api&amp;state=pending&amp;view=recorded&amp;page=3" in html
    assert f"/runs/{RUN}/findings?finding=" in html
    assert '<a href="/decisions?team=checkout-api">Needs a decision · 7</a>' in html
    assert (
        '<a href="/decisions?team=checkout-api&amp;view=recorded" aria-current=page>Recorded · 130</a>'
        in html
    )
    empty = pages.decisions_page([], 0, 1, teams, state="confirmed")
    assert "No decisions match these filters." in empty


def test_decisions_page_lists_findings_that_need_a_decision() -> None:
    rows = [
        {
            **ROW,
            "run_id": RUN,
            "team_id": "checkout-api",
            "team_display_name": "Checkout API",
            "window_start": START,
            "window_end": END,
            "published_at": datetime(2026, 10, 13, 8, 30),
            "finding_id": "R1",
        }
    ]
    teams = [("checkout-api", "Checkout API", True), ("search", "Search", True)]
    html = pages.decisions_page(rows, 51, 1, teams, view="needed", counts=(51, 4))
    no_inline_styles(html)
    assert '<a href="/decisions" aria-current=page>Needs a decision · 51</a>' in html
    assert '<a href="/decisions?view=recorded">Recorded · 4</a>' in html
    assert "No decision" in html and "Published 13 Oct 2026" in html
    assert "Checkout API · 5 Oct \u2013 12 Oct 2026 · payments-api" in html
    assert "Rule finding · v2 · 4,210 events" in html
    assert f"/runs/{RUN}/findings?finding=" in html
    assert 'name="state"' not in html and 'name="view"' not in html, "no decision filter here"
    assert "Page 1 of 2 · 51 findings" in html and '<a href="/decisions?page=2">Next</a>' in html
    empty = pages.decisions_page([], 0, 1, teams, team="search", view="needed")
    assert "Nothing needs a decision for this team." in empty
    assert '<a href="/decisions?team=search" aria-current=page>' in empty


def test_schedule_names_the_next_week_and_each_teams_next_step() -> None:
    overview = {
        "a": {"last": {"outcome": "held", "window_end": END, "invoked_at": END, "run_id": RUN}}
    }
    html = pages.schedule_page(
        [("a", "A", True), ("b", "B", False)], overview, datetime(2026, 10, 14, tzinfo=UTC)
    )
    no_inline_styles(html)
    assert "12 Oct \u2013 19 Oct 2026" in html and "after it ends on 19 Oct" in html
    assert "publishes on its own three days after it was first held" in html
    assert "Not on the weekly schedule" in html and "data-team-selector" not in html


SECRET = "s" * 40


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_admin(AdminSettings(load_config(), "unused", SECRET)))


def form(**fields: Any) -> dict[str, Any]:
    return {"csrf": csrf_token(SECRET, datetime.now(UTC).date()), **fields}


def test_new_runs_with_one_week_submits_a_manual_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []

    def submit_job(settings: Any, body: Any, key: str) -> dict[str, Any]:
        calls.append((body.team, body.run_at, body.llm, key))
        return {"job_id": "a" * 32, "status": "pending"}

    monkeypatch.setattr(console, "submit_job", submit_job)
    response = client.post(
        "/teams/checkout-api/trigger",
        data=form(week="2026-10-12", llm="fake", weeks_form="1", submission_key="k1"),
        follow_redirects=False,
    )
    assert response.status_code == 303 and f"job_id={'a' * 32}" in response.headers["location"]
    team, run_at, llm, key = calls[0]
    assert (team, llm, key) == ("checkout-api", "fake", "k1")
    assert run_at.replace(tzinfo=None) == datetime(2026, 10, 12)


def test_new_runs_with_several_weeks_submits_a_backfill(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []

    def submit_backfill(*args: Any) -> dict[str, Any]:
        calls.append(args[1:])
        return {"backfill_id": "b" * 32, "week_count": 2}

    monkeypatch.setattr(console, "submit_backfill", submit_backfill)
    response = client.post(
        "/teams/checkout-api/trigger",
        data=form(
            week=["2026-10-12", "2026-10-05"], llm="off", weeks_form="1", submission_key="k2"
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303 and "2+runs+accepted" in response.headers["location"]
    team, weeks, llm, key = calls[0]
    assert (team, llm, key) == ("checkout-api", "off", "k2")
    assert [week.replace(tzinfo=None) for week in weeks] == [
        datetime(2026, 10, 5),
        datetime(2026, 10, 12),
    ]


def test_new_runs_without_a_week_is_refused(client: TestClient) -> None:
    response = client.post(
        "/teams/checkout-api/trigger",
        data=form(llm="live", weeks_form="1"),
        follow_redirects=False,
    )
    assert (
        response.status_code == 303
        and "error=Pick+at+least+one+week" in response.headers["location"]
    )


def test_console_query_parameters_are_validated(client: TestClient) -> None:
    assert client.get("/?show=everything").status_code == 422
    assert client.get("/decisions?state=maybe").status_code == 422
    assert client.get("/decisions?view=everything").status_code == 422
    assert client.get("/teams/checkout-api?tab=nonsense").status_code == 422


def test_run_again_buttons_appear_only_where_new_runs_exists() -> None:
    failed = run(status="failed", error_summary="Source unavailable")
    assert 'data-dialog="new-runs"' in pages.failed_panel(failed)
    assert 'data-dialog="new-runs"' not in pages.failed_panel(failed, can_run=False)
    job = {"job_id": "a" * 32, "run_at": END, "status": "failed", "error_summary": "Boom"}
    assert 'data-dialog="new-runs"' in pages.job_panel(job)
    assert 'data-dialog="new-runs"' not in pages.job_panel(job, can_run=False)


def test_only_a_queued_run_offers_remove() -> None:
    cancel = f'action="/run-jobs/{"a" * 32}/cancel"'
    job = {"job_id": "a" * 32, "run_at": END, "status": "queued", "llm_mode": "fake"}
    queued = pages.job_panel(job, token="t")
    assert "is queued" in queued and cancel in queued and "abi-indeterminate" not in queued
    assert "Queued. It starts when the runs ahead of it finish." in queued
    assert "multi-week" not in queued and "multi-week" not in pages.SCRIPT
    assert cancel in pages.jobs_panel([job], token="t")
    assert cancel not in pages.job_panel(job)
    for status in ("pending", "running"):
        active = pages.job_panel(job | {"status": status}, token="t")
        assert "Analyzing" in active and "abi-indeterminate" in active and cancel not in active
        assert cancel not in pages.jobs_panel([job | {"status": status}], token="t")


def test_a_run_skipped_because_its_week_was_published_says_so() -> None:
    job = {"job_id": "a" * 32, "run_at": END, "status": "failed"}
    skipped = pages.job_panel(job | {"error_summary": PUBLISHED_WHILE_WAITING})
    assert "published while the run waited" in skipped
    assert "The published week is unchanged." in skipped and "Nothing was published" not in skipped
    assert "Nothing was published." in pages.job_panel(job | {"error_summary": "Boom"})


def test_run_again_names_its_week_and_explains_when_it_cannot_run() -> None:
    # admin#16 review: the shared dialog must retry the failed week even after another
    # opener changed the selection, so the retry opener carries its own week.
    failed = run(status="failed", error_summary="Source unavailable")
    retry = 'data-dialog="new-runs" data-week="2026-10-12">Run this week again'
    assert retry in pages.failed_panel(failed)
    job = {"job_id": "a" * 32, "run_at": END, "status": "failed", "error_summary": "Boom"}
    assert retry in pages.job_panel(job)
    for html in (
        pages.failed_panel(failed, runnable=False),
        pages.job_panel(job, runnable=False),
    ):
        assert "Run this week again" not in html
        assert "This week can no longer be started here" in html


def test_new_runs_script_tolerates_a_dialog_without_weeks() -> None:
    # Polling setup follows the dialog code, so a missing element must not stop the script.
    assert "if (count) count.textContent" in pages.SCRIPT
    assert "if (foot) foot.textContent" in pages.SCRIPT
    assert "open.dataset.week" in pages.SCRIPT


def test_every_new_runs_opener_resets_the_week_selection() -> None:
    # Cancel and reopen must not keep an earlier selection: an opener with a week checks
    # only that week, any other opener restores the server-rendered default.
    assert "input.checked = week ? input.value === week : input.defaultChecked;" in pages.SCRIPT
    assert "if (open.dataset.week)" not in pages.SCRIPT
