"""The operator console, rendered from SQL with authenticated POST actions.

Design section "Operator console redesign" (2026-10-19): a left sidebar, one card per team,
one week of a team at a time in Run, Review and Publish steps, and one New runs dialog that
starts one week as a manual run or several weeks as a backfill.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from urllib.parse import quote, urlencode
from uuid import uuid4

from alerts_bi_operations.registry import TeamEntry
from alerts_bi_operations.review.decisions import DECISION_STATES, findings_on
from alerts_bi_shared.team_urls import encode_team_id
from alerts_bi_shared.ui.assets import STYLESHEET as SHARED_CSS
from alerts_bi_shared.ui.explain import (
    format_instant,
    format_week,
    principle_next_step,
    principle_title,
    rule_explanation,
)
from alerts_bi_shared.ui.html import h

from .assets import CONSOLE_CSS
from .engine.api.backfill import selectable_weeks
from .engine.api.jobs import PUBLISHED_WHILE_WAITING
from .engine.api.service import latest_week_end
from .engine.weekly.plan import RETENTION_DAYS

STYLESHEET = SHARED_CSS + "\n" + CONSOLE_CSS
STYLESHEET_PATH = f"/assets/console-{sha256(STYLESHEET.encode()).hexdigest()[:12]}.css"
SCRIPT = """(() => {
let opener;
document.addEventListener('click', event => {
 const open = event.target.closest('[data-dialog]');
 if (open) {
  const dialog = document.getElementById(open.dataset.dialog);
  if (dialog) {
   opener = open; event.preventDefault();
   // Every opener starts the week list afresh: its own week, else the page's default.
   const week = open.dataset.week;
   dialog.querySelectorAll('input[name=week]').forEach(input => {
    input.checked = week ? input.value === week : input.defaultChecked;
    input.dispatchEvent(new Event('change', {bubbles: true}));
   });
   dialog.showModal();
  }
 }
 const close = event.target.closest('[data-close]');
 if (close) close.closest('dialog').close();
 document.querySelectorAll('details.abi-picker[open]').forEach(picker => {
  if (!picker.contains(event.target)) picker.open = false;
 });
});
document.addEventListener('keydown', event => {
 if (event.key !== 'Escape') return;
 document.querySelectorAll('details.abi-picker[open]').forEach(picker => {
  picker.open = false; picker.querySelector('summary').focus();
 });
});
document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('close', () => opener?.focus()));
document.querySelectorAll('[data-new-runs]').forEach(form => {
 const submit = form.querySelector('button[type=submit]');
 const count = form.querySelector('[data-week-count]');
 const many = form.querySelector('[data-many]');
 const foot = form.querySelector('[data-foot-note]');
 const busy = form.hasAttribute('data-backfill-active');
 const weeks = () => [...form.querySelectorAll('input[name=week]')];
 const update = () => {
  const picked = weeks().filter(input => input.checked).length;
  if (count) count.textContent = picked + ' of ' + weeks().length + ' selected';
  submit.disabled = picked === 0 || (busy && picked > 1);
  submit.textContent = picked === 0 ? 'Start runs' : picked === 1 ? 'Start 1 run' : 'Start ' + picked + ' runs';
  if (many) many.hidden = picked < 2;
  if (foot) foot.textContent = picked < 2 ? 'The finished run waits for you to publish it.'
   : busy ? 'Several weeks are already running for this team. Pick one week, or wait for them to finish.'
   : 'They run one at a time, oldest first. Each waits for you to publish it.';
 };
 form.addEventListener('change', event => { if (event.target.name === 'week') update(); });
 form.querySelector('[data-select-all]')?.addEventListener('click', () => {
  weeks().forEach(input => { input.checked = true; }); update();
 });
 form.querySelector('[data-clear]')?.addEventListener('click', () => {
  weeks().forEach(input => { input.checked = false; }); update();
 });
 form.addEventListener('submit', () => {
  submit.disabled = true; submit.textContent = 'Starting…';
  form.querySelector('[role=status]').textContent = 'Submitting. Once accepted, you can leave this page.';
 });
 window.addEventListener('pageshow', update);
 update();
});
const JOB_TEXT = {
 pending: 'Waiting for the worker. You can leave this page.',
 queued: 'Queued. It starts when the runs ahead of it finish. You can leave this page.',
 running: 'Analysis running. You can leave this page.'
};
let backfillPolling = false;
async function pollBackfill() {
 const panel = document.querySelector('[data-backfill-url]');
 if (!panel || document.hidden || backfillPolling) return;
 backfillPolling = true;
 try {
  const response = await fetch(panel.dataset.backfillUrl, {headers: {'Accept': 'application/json'}});
  if (!response.ok) throw new Error('Status unavailable');
  const backfill = await response.json();
  const state = backfill.weeks.map(week => week.job_id + ':' + week.status).join(',');
  if (state !== panel.dataset.backfillState) { window.location.reload(); return; }
 } catch (error) {
  panel.querySelector('[role=status]').textContent = 'Connection interrupted. Status checks will retry; this does not mean a run failed.';
 } finally { backfillPolling = false; }
}
if (document.querySelector('[data-backfill-url]')) {
 setInterval(pollBackfill, 3000);
 document.addEventListener('visibilitychange', pollBackfill);
}
let polling = false;
async function pollJobs() {
 if (document.hidden || polling) return;
 polling = true;
 try {
  for (const row of document.querySelectorAll('[data-job-url]')) {
   const response = await fetch(row.dataset.jobUrl, {headers: {'Accept': 'application/json'}});
   if (!response.ok) throw new Error('Status unavailable');
   const job = await response.json();
   const status = row.querySelector('[role=status]');
   status.textContent = JOB_TEXT[job.status] || status.textContent;
   if (job.status !== row.dataset.jobState) {
    const url = new URL(window.location.href);
    if (job.status === 'completed' && url.searchParams.get('job_id') === job.job_id) {
     window.location.assign(job.result_url);
    } else window.location.reload();
    return;
   }
  }
 } catch (error) {
  document.querySelectorAll('[data-job-url] [role=status]').forEach(status => {
   status.textContent = 'Connection interrupted. Status checks will retry; this does not mean the run failed.';
  });
 } finally { polling = false; }
}
if (document.querySelector('[data-job-url]')) {
 setInterval(pollJobs, 3000);
 document.addEventListener('visibilitychange', pollJobs);
 pollJobs();
}
})();"""
SCRIPT_PATH = f"/assets/console-{sha256(SCRIPT.encode()).hexdigest()[:12]}.js"

WEEK = timedelta(days=7)
ACTIVE_JOB = ("pending", "queued", "running")
JOB_MESSAGES = {
    "pending": "Waiting for the worker. You can leave this page.",
    "queued": "Queued. It starts when the runs ahead of it finish. You can leave this page.",
    "running": "Analysis running. You can leave this page.",
}
DECISION_HINTS = {
    "confirmed": "The finding is right",
    "dismissed": "Not a real problem here",
    "pending": "Still checking with the team",
}


# ------------------------------------------------------------------ primitives


def icon(name: str) -> str:
    paths = {
        "teams": '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>',
        "schedule": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
        "decisions": '<path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"/>',
        "arrow": '<path d="M5 12h14m-6-6 6 6-6 6"/>',
        "back": '<path d="M19 12H5m6-6-6 6 6 6"/>',
        "chevron": '<path d="m9 6 6 6-6 6"/>',
        "down": '<path d="m6 9 6 6 6-6"/>',
        "plus": '<path d="M12 5v14M5 12h14"/>',
        "info": '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5v.01"/>',
        "close": '<path d="m6 6 12 12M6 18 18 6"/>',
        "check": '<path d="M5 12l5 5L20 7"/>',
        "calendar": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
        "publish": '<path d="M12 16V4M6 10l6-6 6 6M4 20h16"/>',
    }
    return (
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        f"{paths.get(name, paths['info'])}</svg>"
    )


NAVIGATION = (
    ("teams", "/", "Teams"),
    ("schedule", "/schedule", "Schedule"),
    ("decisions", "/decisions", "Decisions"),
)


def layout(
    title: str,
    body: str,
    flash: str | None = None,
    error: bool = False,
    nav: str = "teams",
    *,
    wide: bool = False,
) -> str:
    navigation = "".join(
        f'<a class="abi-nav" href="{url}"{" aria-current=page" if key == nav else ""}>{icon(key)}{label}</a>'
        for key, url, label in NAVIGATION
    )
    banner = (
        f'<div role="status" class="abi-flash{" error" if error else ""}">{icon("info")}<span>{h(flash)}</span></div>'
        if flash
        else ""
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{h(title)} · Alerts BI</title><link rel="stylesheet" href="{STYLESHEET_PATH}"><script src="{SCRIPT_PATH}" defer></script>'
        '</head><body><div id="abi-console">'
        '<aside class="abi-side"><a class="abi-brand" href="/"><span class="abi-mark" aria-hidden="true"></span>Alerts BI</a>'
        f'<nav class="abi-navs" aria-label="Main">{navigation}</nav></aside>'
        f'<main class="abi-main" id="abi-main"><div class="abi-page{" wide" if wide else ""}">{banner}{body}</div></main>'
        "</div></body></html>"
    )


ERROR_TITLES = {
    400: "Check your request",
    403: "Not allowed",
    404: "Not found",
    409: "Busy right now",
    413: "Too large",
    422: "Check your selection",
}


def error_page(status: int, message: str) -> str:
    return layout(
        str(status),
        f'<section class="abi-panel abi-empty"><p class="abi-error-code">{status}</p>'
        f"<h1>{h(ERROR_TITLES.get(status, 'Something went wrong'))}</h1><p>{h(message)}</p>"
        '<a class="abi-btn" href="/">Back to teams</a></section>',
    )


def team_url(team_id: str, run_id: str | None = None, tab: str = "overview", **query: Any) -> str:
    params = {"tab": tab, **query}
    if run_id:
        params["run_id"] = run_id
    return "/teams/" + encode_team_id(team_id) + "?" + urlencode(params)


def summary_link(team_id: str, run_id: str | None = None) -> str:
    return team_url(team_id, run_id)


def _when(value: Any) -> str:
    return format_instant(value) if isinstance(value, datetime) else "—"


def _short(value: Any) -> str:
    """A short UTC time for cards and steps: ``12 Oct, 03:14 UTC``."""
    if not isinstance(value, datetime):
        return "—"
    return f"{value.day} {value:%b}, {value:%H:%M} UTC"


def _hidden(token: str) -> str:
    return f'<input type="hidden" name="csrf" value="{h(token)}">'


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def publication_state(run: Mapping[str, Any]) -> str:
    return (
        "published"
        if run.get("publication_id")
        else "withdrawn"
        if run.get("withdrawn")
        else "unpublished"
    )


CHIP_TONES = {
    "completed": "good",
    "published": "good",
    "confirmed": "good",
    "failed": "warn",
    "withdrawn": "amber",
    "pending": "amber",
    "held": "amber",
    "queued": "",
    "running": "",
}


def chip(label: str, tone: str = "") -> str:
    return f'<span class="abi-chip{" " + h(tone) if tone else ""}">{h(label)}</span>'


def badge(state: str, tone: str = "") -> str:
    """A state chip; the label is the state, capitalized (v1/v2 kept as written)."""
    label = state if state in ("v1", "v2") else state.replace("_", " ").capitalize()
    return chip(label, tone or CHIP_TONES.get(state, ""))


def _week(run: Mapping[str, Any]) -> str:
    return format_week(run["window_start"], run["window_end"])


def _job_week(job: Mapping[str, Any]) -> str:
    end = _utc(job["run_at"])
    return format_week(end - WEEK, end)


def _source(run: Mapping[str, Any]) -> str:
    if run.get("scheduled"):
        return "Weekly schedule"
    return "Multi-week run" if run.get("backfilled") else "Manual / API"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


as_utc = _utc


def _day_month(value: datetime) -> str:
    return f"{value.day} {value:%b}"


def _post_button(action: str, token: str, label: str, css: str = "abi-link") -> str:
    return (
        f'<form method="post" action="{h(action)}" class="abi-inline-form">{_hidden(token)}'
        f'<button type="submit" class="{css}">{h(label)}</button></form>'
    )


def dialog(
    dialog_id: str,
    title: str,
    action: str,
    token: str,
    content: str,
    submit: str,
    *,
    danger: bool = False,
    note: str = "",
) -> str:
    foot = f'<p class="abi-foot-note">{h(note)}</p>' if note else ""
    return (
        f'<dialog class="abi-dialog" id="{dialog_id}" aria-labelledby="{dialog_id}-title">'
        f'<form method="post" action="{h(action)}">{_hidden(token)}'
        f'<div class="abi-dialog-head"><h2 id="{dialog_id}-title">{h(title)}</h2>'
        f'<button type="button" class="abi-close" data-close aria-label="Close">{icon("close")}</button></div>'
        f'<div class="abi-dialog-body">{content}</div><div class="abi-dialog-foot">{foot}'
        '<button type="button" class="abi-btn" data-close>Cancel</button>'
        f'<button type="submit" class="abi-btn {"danger" if danger else "primary"}">{h(submit)}</button></div>'
        "</form></dialog>"
    )


def pager(
    team_id: str,
    run_id: str | None,
    tab: str,
    page: int,
    total: int,
    size: int,
    param: str = "page",
) -> str:
    if total <= size:
        return ""
    count = max(1, (total + size - 1) // size)
    links = "".join(
        f'<a href="{h(team_url(team_id, run_id, tab, **{param: target}))}">{label}</a>'
        for target, label in ((page - 1, "Previous"), (page + 1, "Next"))
        if 1 <= target <= count
    )
    return f'<div class="abi-pager"><span>Page {page} of {count} · {total:,} total</span><span>{links}</span></div>'


# ------------------------------------------------------------------ Teams


@dataclass(frozen=True, slots=True)
class TeamCard:
    team_id: str
    name: str
    tag: str
    steps: tuple[str, str, str]
    status: str
    tone: str
    attention: bool


def team_card(team_id: str, name: str, enrolled: bool, info: Mapping[str, Any]) -> TeamCard:
    """The newest week of a team as Run, Review and Publish progress with one status line."""
    run = info.get("run")
    active = sorted(info.get("active", ()), key=lambda job: job["run_at"], reverse=True)
    newest = active[0] if active else None
    shown_job = newest is not None and (run is None or newest["run_at"] >= run["run_at"])
    earlier = len(active) - (1 if shown_job else 0)
    steps: tuple[str, str, str] = ("done", "current", "todo")
    tone, attention = "action", True
    if newest is not None and shown_job:
        status = str(newest["status"])
        text = (
            f"Running since {_short(newest.get('started_at'))}"
            if status == "running"
            else "Queued"
            if status == "queued"
            else "Starting"
        )
        steps, tone, attention = ("current", "todo", "todo"), "muted", False
    elif run is None:
        text = "No runs yet"
        steps, tone, attention = ("todo", "todo", "todo"), "muted", False
    elif run["status"] == "failed":
        text, steps, tone = f"Run failed · {_week(run)}", ("fail", "todo", "todo"), "warn"
    elif run["status"] != "completed":
        text = f"Running · {_week(run)}"
        steps, tone, attention = ("current", "todo", "todo"), "muted", False
    elif run.get("publication_id"):
        text = f"Published · {_week(run)}"
        steps, tone, attention = ("done", "done", "done"), "muted", False
    elif run.get("withdrawn"):
        text = f"Withdrawn · {_week(run)}"
    elif run.get("schedule_outcome") == "held":
        unassessed = int(run.get("unassessed") or 0)
        text = f"Held · {unassessed:,} not assessed" if unassessed else "Held"
    else:
        text = f"Ready to review · {_week(run)}"
    if earlier > 0:
        text += f" · {_plural(earlier, 'more week')} in progress"
    return TeamCard(team_id, name, "" if enrolled else "Manual", steps, text, tone, attention)


def _card(card: TeamCard) -> str:
    segments = "".join(f'<span class="abi-seg {state}"></span>' for state in card.steps)
    progress = ", ".join(
        f"{step} {'not started' if state == 'todo' else 'failed' if state == 'fail' else state}"
        for step, state in zip(("Run", "Review", "Publish"), card.steps, strict=True)
    )
    tag = f'<span class="abi-card-tag">{h(card.tag)}</span>' if card.tag else ""
    return (
        f'<li><a class="abi-card" href="{h(team_url(card.team_id))}">'
        f'<span class="abi-card-name"><span>{h(card.name)}</span>{tag}</span>'
        f'<span class="abi-segs" aria-hidden="true"><span class="abi-seg-bar">{segments}</span>'
        '<span class="abi-seg-names"><span>Run</span><span>Review</span><span>Publish</span></span></span>'
        f'<span class="abi-card-status tone-{card.tone}">{h(card.status)}</span>'
        f'<span class="abi-sr">. {h(progress)}</span></a></li>'
    )


def dashboard_page(
    teams: Sequence[tuple[str, str, bool]],
    overview: Mapping[str, Mapping[str, Any]],
    flash: str | None,
    error: bool,
    show: str = "all",
) -> str:
    cards = [
        team_card(team_id, name, enrolled, overview.get(team_id, {}))
        for team_id, name, enrolled in teams
    ]
    cards.sort(key=lambda card: not card.attention)
    attention = [card for card in cards if card.attention]
    shown = attention if show == "attention" else cards
    weekly = sum(1 for _, _, enrolled in teams if enrolled)
    filters = (
        '<nav class="abi-filter" aria-label="Show teams">'
        f'<a href="/"{" aria-current=page" if show != "attention" else ""}>All · {len(cards)}</a>'
        f'<a href="/?show=attention"{" aria-current=page" if show == "attention" else ""}>Needs you · {len(attention)}</a></nav>'
    )
    if shown:
        content = f'<ul class="abi-cards">{"".join(_card(card) for card in shown)}</ul>'
    elif teams:
        content = (
            '<section class="abi-panel abi-empty"><h2>Nothing needs you right now</h2>'
            "<p>No team has a failed run or a finished week waiting to be published.</p>"
            '<a class="abi-btn" href="/">Show all teams</a></section>'
        )
    else:
        content = (
            '<section class="abi-panel abi-empty"><h2>No registered teams</h2>'
            "<p>Add a team to the registry to see it here.</p></section>"
        )
    body = (
        f'<div class="abi-head"><div><h1>Teams</h1><p class="abi-sub">{_plural(len(teams), "team")} · '
        f"{weekly:,} on the weekly schedule</p></div>{filters if teams else ''}</div>{content}"
    )
    return layout("Teams", body, flash, error, wide=True)


# ------------------------------------------------------------------ New runs


_WEEK_STATES = {
    "missing": "No run yet",
    "completed": "Has an unpublished run; picking it runs the week again",
    "published": "Published, locked",
    "active": "Already queued or running",
    "expired": "Past Elasticsearch retention",
    "future": "Week not complete yet",
}


def _expires(end: datetime) -> datetime:
    """When a week's window start leaves the Elasticsearch retention."""
    return end - WEEK + timedelta(days=RETENTION_DAYS)


def week_states(calendar: Mapping[datetime, str], now: datetime) -> dict[datetime, str]:
    """Every Monday window end from the first retained week to the latest completed one."""
    earliest, latest = selectable_weeks(now)
    states = {}
    end = earliest
    while end <= latest:
        states[end] = calendar.get(end.replace(tzinfo=None), "missing")
        end += WEEK
    return states


def runnable_weeks(calendar: Mapping[datetime, str], now: datetime) -> list[datetime]:
    """Retained completed weeks without a publication or active job, newest first."""
    states = week_states(calendar, now)
    return [
        end
        for end, state in sorted(states.items(), reverse=True)
        if state in ("missing", "completed")
    ]


def new_runs_dialog(
    team_id: str,
    token: str,
    name: str,
    calendar: Mapping[datetime, str],
    now: datetime,
    *,
    preselect: datetime | None = None,
    backfill_active: bool = False,
) -> str:
    """One dialog for one or many weeks: every retained week that can run, newest first."""
    states = week_states(calendar, now)
    options = runnable_weeks(calendar, now)
    checked = (
        preselect
        if preselect is not None and preselect in options
        else (options[0] if options else None)
    )
    rows = []
    for end in options:
        expires = _expires(end)
        expiring = expires - _utc(now) <= WEEK
        note = "Unpublished run · runs again" if states[end] == "completed" else "No run"
        if expiring:
            note += f" · leaves retention {_day_month(expires)}"
        rows.append(
            '<li><label class="abi-week-option">'
            f'<input type="checkbox" name="week" value="{end:%Y-%m-%d}"{" checked" if end == checked else ""}>'
            f'<span class="abi-week-name">{h(format_week(end - WEEK, end))}</span>'
            f'<span class="abi-week-note{" warn" if expiring else ""}">{h(note)}</span></label></li>'
        )
    picked = 1 if checked is not None else 0
    if rows:
        weeks = (
            "<fieldset><legend>Weeks to run</legend>"
            '<div class="abi-row abi-between">'
            f'<span class="abi-small abi-muted" aria-live="polite" data-week-count>{picked} of {len(rows)} selected</span>'
            '<span class="abi-row abi-gap-s"><button type="button" class="abi-link" data-select-all>Select all</button>'
            '<button type="button" class="abi-link" data-clear>Clear</button></span></div>'
            f'<ul class="abi-weeks">{"".join(rows)}</ul>'
            '<p class="abi-small abi-muted">Published weeks are locked. Withdraw one to run it again.</p></fieldset>'
        )
    else:
        weeks = (
            '<p class="abi-note">Every week inside the 84-day retention is published, queued or '
            "running. Withdraw a published week to run it again.</p>"
        )
    busy = (
        '<p class="abi-note">Several weeks are already running for this team. You can start one '
        "more week now; it runs before their next week.</p>"
        if backfill_active
        else ""
    )
    content = (
        f'<input type="hidden" name="submission_key" value="{uuid4().hex}">'
        '<input type="hidden" name="weeks_form" value="1">'
        f"{busy}{weeks}"
        '<div class="abi-field"><label for="new-runs-model">Model</label><select id="new-runs-model" name="llm">'
        '<option value="live" selected>Live · configured model</option>'
        '<option value="fake">Fake · deterministic test model</option>'
        '<option value="off">Off · skip assessment</option></select>'
        '<p class="abi-small abi-muted" data-many hidden>Every week is assessed in full, so model use grows with each week.</p></div>'
        '<p role="status" aria-live="polite" class="abi-small"></p>'
    )
    action = "/teams/" + encode_team_id(team_id) + "/trigger"
    disabled = "" if picked else " disabled"
    return (
        f'<dialog class="abi-dialog" id="new-runs" aria-labelledby="new-runs-title">'
        f'<form method="post" action="{h(action)}" data-new-runs{" data-backfill-active" if backfill_active else ""}>{_hidden(token)}'
        f'<div class="abi-dialog-head"><h2 id="new-runs-title">New runs · {h(name or team_id)}</h2>'
        f'<button type="button" class="abi-close" data-close aria-label="Close">{icon("close")}</button></div>'
        f'<div class="abi-dialog-body">{content}</div><div class="abi-dialog-foot">'
        '<p class="abi-foot-note" data-foot-note>The finished run waits for you to publish it.</p>'
        '<button type="button" class="abi-btn" data-close>Cancel</button>'
        f'<button type="submit" class="abi-btn primary"{disabled}>{"Start 1 run" if picked else "Start runs"}</button>'
        "</div></form></dialog>"
    )


def retention_note(
    calendar: Mapping[datetime, str], now: datetime, first_run_end: datetime | None
) -> str:
    """Warn only when a gap in the team's history is about to leave retention.

    The oldest retained week always expires within seven days, so a week counts only when the
    team's first run ended before it: weeks before a team's first run are not a gap.
    """
    if first_run_end is None:
        return ""
    first = _utc(first_run_end)
    urgent = next(
        (
            end
            for end, state in sorted(week_states(calendar, now).items())
            if state == "missing" and end > first and _expires(end) - _utc(now) <= WEEK
        ),
        None,
    )
    if urgent is None:
        return ""
    return (
        f'<div class="abi-note warn">{icon("info")}<p><strong>{h(format_week(urgent - WEEK, urgent))}</strong> '
        f"has no run and leaves Elasticsearch retention on {h(_day_month(_expires(urgent)))}. "
        "After that it can no longer be run.</p>"
        f'<button type="button" class="abi-link" data-dialog="new-runs" data-week="{urgent:%Y-%m-%d}">'
        "Run it</button></div>"
    )


# ------------------------------------------------------------------ several weeks running


def backfill_signature(weeks: Sequence[Mapping[str, Any]]) -> str:
    """Matches the client's comparison of polled week states."""
    return ",".join(f"{week['job_id']}:{week['status']}" for week in weeks)


_GLYPHS = {"completed": "✓", "failed": "!", "running": "", "queued": "", "cancelled": ""}


def _skipped_as_published(job: Mapping[str, Any]) -> bool:
    """The worker failed it without running because its week was published while it waited."""
    return job.get("error_summary") == PUBLISHED_WHILE_WAITING


def _backfill_row(
    team_id: str,
    week: Mapping[str, Any],
    index: int,
    total: int,
    token: str,
    next_up: bool,
    published: bool,
) -> str:
    end = _utc(week["run_at"])
    status = str(week["status"])
    shown = "running" if status == "pending" else status
    job = quote(str(week["job_id"]), safe="")
    action = ""
    if status == "completed":
        detail = f"Finished {_when(week.get('completed_at'))} · waits for you to publish it"
        if week.get("error_summary"):
            detail += f" · {week['error_summary']}"
        url = team_url(team_id, str(week["run_id"]))
        action = f'<a class="abi-btn small" href="{h(url)}">Open run</a>'
    elif status in ("running", "pending"):
        detail = (
            f"Started {_when(week.get('started_at'))} · analysis running"
            if status == "running"
            else "Waiting for the worker"
        )
    elif status == "queued":
        detail = "Next to run" if next_up else "Waits for the week above"
        action = _post_button(f"/run-jobs/{job}/cancel", token, "Remove", "abi-btn small")
    elif status == "failed":
        detail = str(week.get("error_summary") or "Analysis failed.")
        if not published:
            action = _post_button(f"/run-jobs/{job}/retry", token, "Retry week", "abi-btn small")
        elif not _skipped_as_published(week):
            # Retry refuses a published week; the skip message already says what to do.
            detail += " · Published since. Withdraw it to run it again."
    else:
        detail = str(week.get("error_summary") or "Cancelled before it started.")
    attempts = int(week.get("attempts", 1))
    attempt = f" · attempt {attempts}" if attempts > 1 else ""
    label = {"pending": "running"}.get(status, status)
    return (
        f'<li><div class="abi-item"><span class="abi-week-state {shown}" aria-hidden="true">{_GLYPHS.get(shown, "")}</span>'
        f'<span class="abi-item-main"><span class="abi-item-title">{h(format_week(end - WEEK, end))} '
        f'<span class="abi-sr">({h(label)})</span></span>'
        f'<span class="abi-item-sub">Week {index + 1} of {total}{attempt} · {h(detail)}</span></span>{action}</div></li>'
    )


def backfill_panel(
    team_id: str,
    backfill: Mapping[str, Any],
    weeks: Sequence[Mapping[str, Any]],
    token: str,
    *,
    published: Collection[datetime] = (),
) -> str:
    """Progress of the team's newest shown multi-week run, above the selected week.

    ``published`` holds the window ends of failed weeks that are published now: retry refuses
    them, so neither their row nor the panel offers it.
    """
    if not weeks:
        return ""
    base = "/backfills/" + quote(str(backfill["backfill_id"]), safe="")
    count = dict.fromkeys(("completed", "running", "queued", "failed", "cancelled"), 0)
    for week in weeks:
        count["running" if week["status"] == "pending" else str(week["status"])] += 1
    total = len(weeks)
    active = count["running"] + count["queued"] > 0
    if count["running"]:
        tone, state_label = "", "Running"
    elif active:
        tone, state_label = "", "Queued"
    elif count["failed"]:
        tone, state_label = "warn", f"Finished with {count['failed']} failed"
    elif count["cancelled"] and not count["completed"]:
        tone, state_label = "", "Cancelled"
    else:
        tone, state_label = "good", "Finished"
    actions = ""
    if count["queued"]:
        weeks_label = "week" if count["queued"] == 1 else "weeks"
        actions += _post_button(
            base + "/cancel",
            token,
            f"Cancel {count['queued']} queued {weeks_label}",
            "abi-btn small",
        )
    retryable = sum(
        1 for week in weeks if week["status"] == "failed" and _utc(week["run_at"]) not in published
    )
    if not active and retryable:
        retry = "Retry failed week" if retryable == 1 else "Retry failed weeks"
        actions += _post_button(base + "/retry", token, retry, "abi-btn small")
    if not active:
        actions += _post_button(base + "/dismiss", token, "Dismiss", "abi-btn small")
    parts = [f"{count['completed']} of {total} completed"] + [
        f"{count[state]} {state}"
        for state in ("running", "queued", "failed", "cancelled")
        if count[state]
    ]
    if active:
        message = (
            "You can leave this page. Queued weeks start on their own, oldest first. "
            "A single new run or the weekly schedule goes ahead of the next queued week."
        )
    elif count["completed"]:
        message = "Completed weeks are ready to review and publish."
    else:
        message = "No week completed."
    if backfill.get("cancelled_at"):
        message += f" Queued weeks cancelled · {_when(backfill['cancelled_at'])}."
    first_queued = next(
        (index for index, week in enumerate(weeks) if week["status"] == "queued"), None
    )
    rows = "".join(
        _backfill_row(
            team_id,
            week,
            index,
            total,
            token,
            index == first_queued and not count["running"],
            _utc(week["run_at"]) in published,
        )
        for index, week in enumerate(weeks)
    )
    polling = (
        f' data-backfill-url="{h(base)}" data-backfill-state="{h(backfill_signature(weeks))}"'
        if active
        else ""
    )
    title = f"{'Running' if active else 'Ran'} {_plural(total, 'week')}"
    return (
        f'<section class="abi-panel tight" id="backfill" aria-labelledby="backfill-title"{polling}>'
        f'<div class="abi-row abi-between"><h2 id="backfill-title">{title}</h2>{chip(state_label, tone)}</div>'
        f'<div class="abi-stack abi-gap-s"><p class="abi-small"><strong>{h(" · ".join(parts))}</strong></p>'
        f'<progress class="abi-bar" max="{total}" value="{count["completed"]}" aria-label="Weeks completed">'
        f"{count['completed']} of {total}</progress></div>"
        f'<ul class="abi-list">{rows}</ul>'
        f'<div class="abi-panel-foot"><div><p role="status" aria-live="polite" class="abi-small abi-muted">{h(message)}</p>'
        f'<p class="abi-tiny abi-muted">Started {h(_when(backfill["submitted_at"]))} '
        f"· Model {h(backfill['llm_mode'])}</p></div>"
        f'<span class="abi-actions">{actions}</span></div></section>'
    )


# ------------------------------------------------------------------ a team's week


def _picker_state(run: Mapping[str, Any]) -> tuple[str, str]:
    if run["status"] == "failed":
        return "failed", "Failed"
    if run["status"] != "completed":
        return "", str(run["status"]).capitalize()
    state = publication_state(run)
    if state == "published":
        return "published", "Published"
    if state == "withdrawn":
        return "withdrawn", "Withdrawn"
    return "unpublished", "Not published"


def week_picker(
    team_id: str,
    runs: Sequence[Mapping[str, Any]],
    selected_id: str | None,
    label: str,
    run_page: int,
    total: int,
) -> str:
    """The team's runs, newest first, twenty per page, as a native disclosure."""
    items = []
    for run in runs:
        run_id = str(run["run_id"])
        dot, state = _picker_state(run)
        current = "true" if run_id == selected_id else "false"
        url = team_url(team_id, run_id, run_page=run_page)
        items.append(
            f'<li><a href="{h(url)}" aria-current="{current}"><span class="abi-dot {dot}" aria-hidden="true"></span>'
            f'<span class="abi-pop-week">{h(_week(run))}</span><span class="abi-pop-state">{h(state)}</span></a></li>'
        )
    pages = max(1, (total + 19) // 20)
    foot = "".join(
        f'<a href="{h(team_url(team_id, selected_id, run_page=target))}">{text}</a>'
        for target, text in ((run_page - 1, "Newer runs"), (run_page + 1, "Older runs"))
        if 1 <= target <= pages
    )
    return (
        f'<details class="abi-picker"><summary aria-label="Week: {h(label)}. Change week">{icon("calendar")}'
        f'<span>{h(label)}</span>{icon("down")}</summary><div class="abi-pop">'
        f'<p class="abi-pop-title">{_plural(total, "run")} · newest first</p><ul>{"".join(items)}</ul>'
        f"{f'<div class="abi-pop-foot">{foot}</div>' if foot else ''}</div></details>"
    )


def _step(name: str, state: str, text: str) -> str:
    current = state in ("current", "running", "fail")
    dot = {
        "done": icon("check"),
        "fail": "!",
        "running": "",
    }.get(state, {"Run": "1", "Review": "2", "Publish": "3"}[name])
    css = {"current": "", "running": "running", "fail": "fail", "done": "done", "todo": "todo"}[
        state
    ]
    return (
        f'<li class="abi-step {css}"{" aria-current=step" if current else ""}>'
        f'<span class="abi-step-dot" aria-hidden="true">{dot}</span>'
        f'<span><span class="abi-step-name">{name}</span><span class="abi-step-text">{h(text)}</span></span></li>'
    )


def week_steps(selected: Mapping[str, Any] | None, job: Mapping[str, Any] | None, view: str) -> str:
    """Run, Review and Publish for the selected run or submission."""
    if job is not None:
        status = str(job["status"])
        if status == "failed":
            run = ("fail", f"Failed · {_short(job.get('completed_at'))}")
        elif status == "running":
            run = ("running", f"Running since {_short(job.get('started_at'))}")
        else:
            run = ("current", "Queued" if status == "queued" else "Waiting for the worker")
        review, publish = ("todo", "After the run"), ("todo", "Waiting")
    elif selected is None:
        return ""
    elif selected["status"] == "failed":
        run = ("fail", f"Failed · {_short(selected.get('completed_at'))}")
        review, publish = ("todo", "After a good run"), ("todo", "Waiting")
    elif selected["status"] != "completed":
        run = ("current", "In progress")
        review, publish = ("todo", "After the run"), ("todo", "Waiting")
    else:
        run = ("done", f"Done · {_short(selected.get('completed_at'))}")
        state = publication_state(selected)
        by_schedule = selected.get("published_by") == "weekly-schedule"
        if state == "published":
            review = ("done", "Checked automatically" if by_schedule else "Done")
            publish = (
                "done",
                "Done · by the schedule"
                if by_schedule
                else f"Done · {_short(selected.get('published_at'))}",
            )
        elif view == "publish":
            review, publish = ("done", "Done"), ("current", "Ready")
        else:
            held = selected.get("schedule_outcome") == "held"
            unassessed = int(selected.get("unassessed") or 0)
            count = int(selected.get("findings_count") or 0)
            if state == "withdrawn":
                text = "Withdrawn"
            elif held:
                text = f"Held · {unassessed:,} not assessed" if unassessed else "Held"
            else:
                text = f"{_plural(count, 'finding')}"
            review = ("current", text)
            publish = ("todo", "On its own after 3 days held" if held else "When you're ready")
    return (
        '<ol class="abi-steps" aria-label="Steps for this week">'
        + _step("Run", *run)
        + _step("Review", *review)
        + _step("Publish", *publish)
        + "</ol>"
    )


def _run_again(end: datetime, *, can_run: bool, runnable: bool) -> str:
    """Retry one week: the opener checks only that week in the New runs dialog."""
    if not can_run:
        return ""
    if not runnable:
        return (
            '<span class="abi-small abi-muted">This week can no longer be started here: it is '
            "published, already queued or running, or past the 84-day retention.</span>"
        )
    return (
        '<button type="button" class="abi-btn primary" data-dialog="new-runs" '
        f'data-week="{_utc(end):%Y-%m-%d}">Run this week again</button>'
    )


def _remove_queued(job: Mapping[str, Any], token: str) -> str:
    """Take a queued manual run out of the queue; nothing to offer once it has started."""
    if str(job["status"]) != "queued" or not token:
        return ""
    return _post_button(f"/run-jobs/{job['job_id']}/cancel", token, "Remove", "abi-btn small")


def job_panel(
    job: Mapping[str, Any], *, token: str = "", can_run: bool = True, runnable: bool = True
) -> str:
    """The selected manual submission while it waits or runs, or after it failed."""
    week = _job_week(job)
    status = str(job["status"])
    facts = (
        '<dl class="abi-facts">'
        f"<div><dt>Submitted</dt><dd>{h(_when(job.get('submitted_at')))}</dd></div>"
        f"<div><dt>Model</dt><dd>{h(str(job.get('llm_mode') or '—').capitalize())}</dd></div></dl>"
    )
    if status == "failed":
        outcome = (
            "The published week is unchanged."
            if _skipped_as_published(job)
            else "Nothing was published."
        )
        return (
            f'<section class="abi-panel" aria-labelledby="step-title"><h2 id="step-title">The run of {h(week)} failed</h2>'
            f'<p class="abi-message">{h(job.get("error_summary") or "Analysis failed.")}</p>{facts}'
            f'<div class="abi-panel-foot"><span class="abi-small abi-muted">{outcome}</span>'
            f"{_run_again(job['run_at'], can_run=can_run, runnable=runnable)}</div></section>"
        )
    if status == "queued":
        heading = f"{week} is queued"
        intro = (
            "Runs go one at a time across all teams, so this one waits for the runs ahead of it."
        )
        progress = ""
    else:
        heading = f"Analyzing {week}"
        intro = "Reading the week's alerts and checking them against the rules and the model."
        progress = '<div class="abi-indeterminate" aria-hidden="true"><span></span></div>'
    return (
        f'<section class="abi-panel" aria-labelledby="step-title" data-job-url="/run-jobs/{h(job["job_id"])}" data-job-state="{h(status)}">'
        f'<h2 id="step-title">{h(heading)}</h2><p class="abi-sub">{h(intro)}</p>{progress}'
        f'{facts}<div class="abi-panel-foot"><p role="status" aria-live="polite" class="abi-small abi-muted">{h(JOB_MESSAGES.get(status, ""))}</p>'
        f"{_remove_queued(job, token)}</div></section>"
    )


def jobs_panel(jobs: Sequence[Mapping[str, Any]], shown: str | None = None, token: str = "") -> str:
    """Other manual submissions that are active or failed, polled while active."""
    rows = []
    for job in jobs:
        if str(job["job_id"]) == shown:
            continue
        status = str(job["status"])
        active = status in ACTIVE_JOB
        attrs = (
            f' data-job-url="/run-jobs/{h(job["job_id"])}" data-job-state="{h(status)}"'
            if active
            else ""
        )
        message = (
            JOB_MESSAGES[status]
            if active
            else str(job.get("error_summary") or "Analysis failed. Start it again from New runs.")
        )
        rows.append(
            f'<li{attrs}><div class="abi-item"><span class="abi-item-main"><span class="abi-item-title">{h(_job_week(job))}</span>'
            f'<span class="abi-item-sub" role="status" aria-live="polite">{h(message)}</span></span>'
            f"{badge(status)}{_remove_queued(job, token)}</div></li>"
        )
        if len(rows) == 5:
            break
    if not rows:
        return ""
    return (
        '<section class="abi-panel tight" aria-labelledby="jobs-title"><h2 id="jobs-title">Run submissions</h2>'
        f'<ul class="abi-list">{"".join(rows)}</ul></section>'
    )


def _finding_text(row: Mapping[str, Any], finding_id: str) -> tuple[str, str, str, str, str]:
    if not finding_id.startswith("R"):
        return (
            principle_title(finding_id),
            str(row.get("llm_justification") or "No model explanation was stored."),
            principle_next_step(finding_id),
            "Needs review" if row.get("quality_state") == "needs_review" else "Model advisory",
            "",
        )
    try:
        evidence = json.loads(str(row.get("findings_evidence") or "[]"))
    except (ValueError, TypeError):
        evidence = []
    sample = (
        next(
            (
                item
                for item in evidence
                if isinstance(item, dict) and item.get("rule_id") == finding_id
            ),
            {},
        )
        if isinstance(evidence, list)
        else {}
    )
    # The persisted evidence contains a representative sample nested beneath the rule.
    sample = sample.get("sample_evidence", sample)
    explanation = rule_explanation(finding_id, sample if isinstance(sample, dict) else None)
    return (
        explanation.title,
        explanation.why,
        explanation.next_step,
        "Readiness gap" if explanation.readiness else "Rule finding",
        "amber" if explanation.readiness else "",
    )


def finding_key(row: Mapping[str, Any], finding_id: str) -> str:
    identity = [str(row.get(k, "")) for k in ("alert_schema", "application", "key_field")]
    return sha256(json.dumps([*identity, finding_id]).encode()).hexdigest()[:24]


def _identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(row["alert_schema"]), str(row["application"]), str(row["key_field"]))


identity = _identity


def findings_list(
    run: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    total: int,
    page: int,
    history: Mapping[tuple[str, str, str], Sequence[Mapping[str, Any]]],
    *,
    decisions: bool,
) -> str:
    """One page of the run's findings; each opens the finding page."""
    team_id, run_id = str(run["team_id"]), str(run["run_id"])
    choices = [(row, finding) for row in rows for finding in findings_on(dict(row))]
    if not choices:
        return '<p class="abi-muted">No findings this week.</p>' + pager(
            team_id, run_id, "findings", page, total, 50
        )
    items = []
    for row, finding in choices:
        title, _, _, kind, tone = _finding_text(row, finding)
        url = team_url(team_id, run_id, "findings", page=page, finding=finding_key(row, finding))
        chips = chip(kind, tone) + chip(str(row["alert_schema"]))
        if decisions:
            past = [
                d
                for d in history.get(_identity(row), [])
                if d["finding_id"] == finding and str(d["run_id"]) == run_id
            ]
            chips += badge(str(past[-1]["state"])) if past else chip("No decision", "none")
        items.append(
            f'<li><a class="abi-item" href="{h(url)}"><span class="abi-item-main">'
            f'<span class="abi-item-title">{h(title)}</span>'
            f'<span class="abi-item-sub">{h(row.get("component") or row["application"])} · {_plural(int(row["row_count"]), "event")}</span></span>'
            f'<span class="abi-item-chips">{chips}</span>{icon("chevron")}</a></li>'
        )
    return f'<ul class="abi-list">{"".join(items)}</ul>' + pager(
        team_id, run_id, "findings", page, total, 50
    )


def review_panel(
    run: Mapping[str, Any],
    totals: str,
    findings: str,
    total: int,
    *,
    withdrawal: Mapping[str, Any] | None = None,
) -> str:
    """Step 2: what the run found, then on to publishing."""
    team_id, run_id = str(run["team_id"]), str(run["run_id"])
    notes = ""
    if withdrawal is not None:
        reason = withdrawal.get("note") or ""
        notes += (
            f'<div class="abi-note">{icon("info")}<p>Withdrawn '
            f"on {h(_when(withdrawal.get('at')))}{': ' + h(reason) if reason else '.'} "
            "Its findings and decisions stay here. Publish it again when it is ready.</p></div>"
        )
    if run.get("schedule_outcome") == "held":
        unassessed = int(run.get("unassessed") or 0)
        detail = run.get("schedule_detail") or ""
        cause = (
            f"The model couldn't assess {_plural(unassessed, 'alert')}, so this week is held."
            if unassessed
            else f"This week is held{': ' + str(detail) if detail else '.'}"
        )
        notes += (
            f'<div class="abi-note">{icon("info")}<p>{h(cause)} The schedule retries it every day '
            "and publishes it on its own three days after it was first held, with a note for readers.</p></div>"
        )
    publish = team_url(team_id, run_id, "publish")
    return (
        '<section class="abi-panel" aria-labelledby="step-title"><h2 id="step-title">Review findings</h2>'
        f"{notes}{totals}<div>{findings}</div>"
        f'<div class="abi-panel-foot"><span class="abi-small abi-muted">{_plural(total, "alert")} with findings</span>'
        f'<a class="abi-btn primary" href="{h(publish)}">Continue to publish{icon("arrow")}</a></div></section>'
    )


def publish_panel(
    run: Mapping[str, Any],
    token: str,
    *,
    alerts: int,
    flagged: int,
    unassessed: int,
    replace: bool,
) -> str:
    """Step 3: confirm and publish the selected run."""
    team_id, run_id = str(run["team_id"]), str(run["run_id"])
    action = "/runs/" + quote(run_id, safe="") + "/publish"
    note = (
        f'<p class="abi-small">The {_plural(unassessed, "alert")} the model couldn\'t assess show as '
        '"Not reviewed" in the portal.</p>'
        if unassessed
        else ""
    )
    replacing = (
        '<label class="abi-check"><input type="checkbox" name="replace" value="1" required>'
        "<span>Replace the run that is published for this week. Its publication is withdrawn.</span></label>"
        if replace
        else ""
    )
    return (
        f'<section class="abi-panel" aria-labelledby="step-title"><h2 id="step-title">Publish {h(_week(run))}</h2>'
        '<p class="abi-sub">Readers see this week in the portal as soon as you publish.</p>'
        '<dl class="abi-stats">'
        f"<div><dt>alerts</dt><dd>{alerts:,}</dd></div>"
        f"<div><dt>with findings</dt><dd>{flagged:,}</dd></div>"
        f"<div><dt>not assessed by the model</dt><dd>{unassessed:,}</dd></div></dl>{note}"
        f'<form method="post" action="{h(action)}" class="abi-stack">{_hidden(token)}'
        '<div class="abi-field"><label for="review-note">Note for readers · optional</label>'
        '<textarea id="review-note" name="note" maxlength="4000" placeholder="Add context for this publication"></textarea></div>'
        f"{replacing}"
        '<div class="abi-panel-foot"><span class="abi-small abi-muted">You can withdraw it later. Its history is kept.</span>'
        f'<span class="abi-actions"><a class="abi-btn" href="{h(team_url(team_id, run_id))}">Back to review</a>'
        f'<button type="submit" class="abi-btn primary">{icon("publish")}Publish week</button></span></div>'
        "</form></section>"
    )


def published_panel(run: Mapping[str, Any], totals: str, findings: str) -> str:
    """A published week: where readers see it, and the human decisions on its findings."""
    # The weekly scheduler is the only publisher still named; people are not recorded.
    who = " by the weekly schedule" if run.get("published_by") == "weekly-schedule" else ""
    note = run.get("review_note")
    return (
        f'<div class="abi-note good">{icon("check")}<p>In the portal since {h(_when(run.get("published_at")))}. '
        f"Published{h(who)}{'. Note for readers: ' + h(note) if note else '.'}</p>"
        '<button type="button" class="abi-link" data-dialog="withdraw-review">Withdraw</button></div>'
        '<section class="abi-panel" aria-labelledby="findings-title"><h2 id="findings-title">Findings and decisions</h2>'
        '<p class="abi-sub">Record what you think of a finding. Your decision sits next to the automated '
        f"result and never changes it.</p>{totals}<div>{findings}</div></section>"
    )


def failed_panel(run: Mapping[str, Any], *, can_run: bool = True, runnable: bool = True) -> str:
    """A run that did not finish: its error and a way to run the week again."""
    retry = (
        "The schedule tries again every day. " if run.get("scheduled") else ""
    ) + "Only completed runs can be published."
    return (
        '<section class="abi-panel" aria-labelledby="step-title"><h2 id="step-title">The run didn\'t finish</h2>'
        f'<p class="abi-message">{h(run.get("error_summary") or "No error was recorded.")}</p>'
        f'<div class="abi-panel-foot"><span class="abi-small abi-muted">{h(retry)}</span>'
        f"{_run_again(run['window_end'], can_run=can_run, runnable=runnable)}</div></section>"
    )


def other_status_panel(run: Mapping[str, Any]) -> str:
    return (
        f'<section class="abi-panel" aria-labelledby="step-title"><h2 id="step-title">Analysis {h(run["status"])}</h2>'
        f"<p>{h(run.get('error_summary') or 'No completed analysis is available.')}</p>"
        '<p class="abi-small abi-muted">Only completed runs can be published.</p></section>'
    )


def empty_team_panel(enrolled: bool, has_weeks: bool) -> str:
    when = (
        "The weekly schedule reviews this team after the next Monday. "
        if enrolled
        else "This team is not on the weekly schedule. "
    )
    button = (
        f'<button type="button" class="abi-btn primary" data-dialog="new-runs">{icon("plus")}New runs</button>'
        if has_weeks
        else ""
    )
    return (
        f'<section class="abi-panel abi-empty" aria-labelledby="empty-title"><span class="abi-empty-icon">{icon("calendar")}</span>'
        '<h2 id="empty-title">No runs yet</h2>'
        f"<p>{h(when)}You can run this week and earlier weeks now.</p>{button}</section>"
    )


def _activity(
    run: Mapping[str, Any], publications: Sequence[Mapping[str, Any]]
) -> list[tuple[Any, str, str]]:
    events: list[tuple[Any, str, str]] = [
        (run.get("started_at") or run["run_at"], "Analysis started", _source(run))
    ]
    if run.get("completed_at"):
        events.append(
            (
                run["completed_at"],
                f"Analysis {run['status']}",
                str(run.get("error_summary") or "Results stored for review."),
            )
        )
    for row in publications:
        events.append(
            (
                row["published_at"],
                "Published by the weekly schedule"
                if row.get("published_by") == "weekly-schedule"
                else "Published",
                str(row.get("review_note") or "Visible in the reader portal."),
            )
        )
        if row.get("withdrawn_at"):
            events.append(
                (
                    row["withdrawn_at"],
                    "Withdrawn",
                    str(row.get("withdrawn_reason") or ""),
                )
            )
    return sorted(events, key=lambda item: item[0], reverse=True)


def schedule_log(logs: Sequence[Mapping[str, Any]]) -> str:
    items = "".join(
        f"<li><span>{badge(str(row['outcome']))} {h(format_week(_utc(row['window_end']) - WEEK, _utc(row['window_end'])) if row.get('window_end') else '')}</span>"
        f'<span class="abi-muted">{h(_when(row["invoked_at"]))}</span>'
        f'<span class="abi-activity-detail">{h(row.get("detail") or "")}</span></li>'
        for row in logs
    )
    return (
        '<details class="abi-sub-more"><summary>Team schedule log</summary>'
        f'<ol class="abi-activity">{items or "<li>No schedule events yet.</li>"}</ol></details>'
    )


def log_section(logs: Sequence[Mapping[str, Any]]) -> str:
    """The team's schedule log on its own, for a team without runs."""
    return f'<section class="abi-panel tight">{schedule_log(logs)}</section>'


def details_section(
    run: Mapping[str, Any],
    publications: Sequence[Mapping[str, Any]],
    logs: Sequence[Mapping[str, Any]],
    analysis: str = "",
    *,
    open_analysis: bool = False,
) -> str:
    """Run details and activity, collapsed below the week; the full analysis lives here."""
    facts = "".join(
        f"<div><dt>{label}</dt><dd{' class="abi-mono"' if mono else ''}>{h(value or '—')}</dd></div>"
        for label, value, mono in (
            ("Run", str(run["run_id"]), True),
            ("Window (UTC)", f"{_when(run['window_start'])} → {_when(run['window_end'])}", False),
            ("Started by", _source(run), False),
            ("Sources", run.get("sources_label"), False),
            ("Model", run.get("model_version"), False),
            ("Ruleset", run.get("ruleset_version"), False),
            ("Prompt", run.get("prompt_version"), False),
        )
    )
    events = "".join(
        f'<li><span>{h(title)}</span><span class="abi-muted">{h(_when(at))}</span>'
        f'<span class="abi-activity-detail">{h(detail)}</span></li>'
        for at, title, detail in _activity(run, publications)
    )
    detailed = (
        f'<details class="abi-sub-more" id="analysis"{" open" if open_analysis else ""}>'
        "<summary>Detailed analysis, charts and CSV downloads</summary>"
        f'<div class="abi-legacy">{analysis}</div></details>'
        if analysis
        else ""
    )
    return (
        f'<details class="abi-more" id="run-details"{" open" if open_analysis else ""}>'
        '<summary>Run details and activity</summary><div class="abi-more-body">'
        f'<dl class="abi-facts">{facts}</dl>'
        f'<div><h3>Activity</h3><ol class="abi-activity">{events}</ol></div>'
        f"{schedule_log(logs)}{detailed}"
        '<div><button type="button" class="abi-link danger" data-dialog="delete-run">Delete run</button></div>'
        "</div></details>"
    )


def withdraw_dialog(run: Mapping[str, Any], token: str) -> str:
    content = (
        f"<p>{h(run.get('team_display_name') or run['team_id'])}'s week {h(_week(run))} leaves the portal. "
        "Its findings, decisions and history stay here, and you can publish it again later.</p>"
        '<div class="abi-field"><label for="withdraw-reason">Reason</label>'
        '<textarea id="withdraw-reason" name="reason" required maxlength="1000" '
        'placeholder="Why should readers stop seeing this week?"></textarea></div>'
    )
    return dialog(
        "withdraw-review",
        f"Withdraw {_week(run)}?",
        "/runs/" + quote(str(run["run_id"]), safe="") + "/withdraw",
        token,
        content,
        "Withdraw week",
        danger=True,
    )


def delete_dialog(run: Mapping[str, Any], token: str) -> str:
    run_id = str(run["run_id"])
    content = (
        f'<div class="abi-context"><strong>{h(run.get("team_display_name") or run["team_id"])} · {h(_week(run))}</strong>'
        f'<span class="abi-mono">Run {h(run_id[:16])}</span></div>'
        "<p>This permanently deletes the run, its results, publications, decisions and activity. "
        "If the week is published, it also leaves the portal. The weekly schedule may run the week again.</p>"
        '<label class="abi-check"><input type="checkbox" name="confirm_delete" value="1" required>'
        "<span>I understand and want to permanently delete this run</span></label>"
    )
    return dialog(
        "delete-run",
        "Delete this run?",
        "/runs/" + quote(run_id, safe="") + "/delete",
        token,
        content,
        "Delete run",
        danger=True,
    )


#: A run's operators as its frozen registry entry listed them: (v1 operators, v2 operator).
Operators = tuple[tuple[str, ...], str | None]


def _operator_chips(values: Sequence[str]) -> str:
    return (
        '<ul class="abi-op-list">'
        + "".join(f'<li><span class="abi-chip abi-mono">{h(v)}</span></li>' for v in values)
        + "</ul>"
    )


def _effective(value: Any) -> str:
    try:
        day = datetime.strptime(str(value), "%Y-%m-%d")
    except ValueError:
        return ""
    return f" · effective {day.day} {day:%b %Y}"


def operators_panel(
    team_id: str,
    teams: Sequence[TeamEntry],
    registry_version: str,
    effective_date: Any,
    selected: Mapping[str, Any] | None,
    frozen: Operators | None,
) -> str:
    """The team's operators from the current registry, with the selected run's when they differ.

    Design "Team operators on the team page" (2026-10-21).
    """
    entry = next((team for team in teams if team.team_id == team_id), None)
    current: Operators | None = (entry.v1_operators, entry.v2_operator) if entry else None
    shown = current or frozen
    notes = []
    if entry is None:
        notes.append(
            "This team is not in the current registry."
            + (" These are the operators the selected week ran with." if frozen else "")
        )
    elif not entry.v1_operators and not entry.v2_operator:
        notes.append(
            "This team has no operators in the registry, so a run finds no alerts for it. "
            "Add its v1 operators or v2 operator to the team registry."
        )
    if current is not None and frozen is not None and frozen != current and selected:
        listed = [*frozen[0], *([frozen[1]] if frozen[1] else [])]
        notes.append(
            f"The selected week ({_week(selected)}) ran with registry "
            f"{selected.get('registry_version') or 'unknown'}, which listed "
            + (
                f"{_plural(len(listed), 'operator')}: {', '.join(listed)}"
                if listed
                else "no operators"
            )
            + ". Run the week again to use the current list."
        )
    lines = []
    if entry is not None:
        # The v1 and v2 schemas are separate sources, so an operator is shared only within one.
        listed_by = [(op, "v1") for op in dict.fromkeys(entry.v1_operators)]
        listed_by += [(entry.v2_operator, "v2")] if entry.v2_operator else []
        for operator, schema in listed_by:
            others = [
                team.display_name
                for team in teams
                if team.team_id != team_id
                and (
                    operator in team.v1_operators
                    if schema == "v1"
                    else operator == team.v2_operator
                )
            ]
            if others:
                lines.append(
                    f'<p class="abi-small abi-muted"><span class="abi-mono">{h(operator)}</span> '
                    f"is also listed for {h(', '.join(others))}. Its alerts count for each team.</p>"
                )
    lists = ""
    if shown is not None:
        v1, v2 = shown
        lists = (
            '<dl class="abi-ops">'
            f"<div><dt>v1 operators ({len(v1):,})</dt><dd>"
            + (_operator_chips(v1) if v1 else '<span class="abi-chip none">None</span>')
            + "</dd></div><div><dt>v2 operator</dt><dd>"
            + (_operator_chips([v2]) if v2 else '<span class="abi-chip none">Not set yet</span>')
            + "</dd></div></dl>"
        )
    return (
        '<section class="abi-panel tight" aria-labelledby="operators-title">'
        '<div class="abi-row abi-between"><h2 id="operators-title" class="abi-h3">Operators</h2>'
        f'<span class="abi-small abi-muted">Registry {h(registry_version)}{_effective(effective_date)}</span></div>'
        + lists
        + "".join(f'<div class="abi-note">{icon("info")}<p>{h(text)}</p></div>' for text in notes)
        + "".join(lines)
        + '<p class="abi-tiny abi-muted">Alerts are matched to the team by these exact, '
        "case-sensitive values. Change them in the team registry.</p></section>"
    )


def team_page(
    team_id: str,
    name: str,
    enrolled: bool,
    runs: Sequence[Mapping[str, Any]],
    total: int,
    run_page: int,
    selected: Mapping[str, Any] | None,
    content: str,
    flash: str | None,
    error: bool,
    *,
    view: str = "review",
    job: Mapping[str, Any] | None = None,
    jobs: str = "",
    backfill: str = "",
    new_runs: str = "",
    note: str = "",
    operators: str = "",
    details: str = "",
    dialogs: str = "",
) -> str:
    """One week of a team: header, operators, notes, progress steps, the current step and details."""
    selected_id = str(selected["run_id"]) if selected else None
    label = _job_week(job) if job is not None else _week(selected) if selected else ""
    picker = (
        week_picker(team_id, runs, None if job is not None else selected_id, label, run_page, total)
        if runs
        else ""
    )
    new_button = (
        f'<button type="button" class="abi-btn" data-dialog="new-runs">{icon("plus")}New runs</button>'
        if new_runs
        else ""
    )
    schedule = "Weekly · Monday to Monday UTC" if enrolled else "Manual runs · Monday to Monday UTC"
    body = (
        f'<a class="abi-back" href="/">{icon("back")}Teams</a>'
        f'<div class="abi-head"><div><h1>{h(name)}</h1><p class="abi-sub">{schedule}</p></div>'
        f'<div class="abi-actions">{picker}{new_button}</div></div>'
        f"{operators}{note}{backfill}{jobs}{week_steps(selected, job, view)}{content}{details}"
    )
    return layout(name, body + new_runs + dialogs, flash, error)


def finding_page(
    team_name: str,
    run: Mapping[str, Any],
    row: Mapping[str, Any],
    finding: str,
    history: Sequence[Mapping[str, Any]],
    token: str,
    page: int,
    flash: str | None,
    error: bool,
) -> str:
    """One finding full width: evidence on the left, the human decision on the right."""
    team_id, run_id = str(run["team_id"]), str(run["run_id"])
    title, why, fix, kind, tone = _finding_text(row, finding)
    past = [d for d in history if d["finding_id"] == finding]
    current = [d for d in past if str(d["run_id"]) == run_id]
    latest = current[-1] if current else None
    if latest is not None:
        decision = (
            f'<div class="abi-row">{badge(str(latest["state"]))}<span class="abi-small abi-muted">'
            f"{h(_when(latest['decided_at']))}</span></div>"
            f'<p class="abi-small">{h(latest["note"])}</p>'
        )
    else:
        decision = '<p class="abi-small abi-muted">No decision for this week yet.</p>'
    dialogs = ""
    if run.get("publication_id"):
        decision += '<button type="button" class="abi-btn primary" data-dialog="record-decision">Record decision</button>'
        dialogs = decision_dialog(run, row, finding, title, token, page)
    else:
        decision += (
            '<p class="abi-small abi-muted">Publish this run before recording a decision.</p>'
        )
    shown = [d for d in reversed(past) if d is not latest]
    history_html = (
        '<h3 class="abi-small">Decision history</h3><ol class="abi-history">'
        + "".join(
            f'<li><div class="abi-row abi-between">{badge(str(d["state"]))}'
            f'<span class="abi-tiny abi-muted">Run {h(str(d["run_id"])[:12])}</span></div>'
            f'<p class="abi-small">{h(d["note"])}</p>'
            f'<p class="abi-tiny abi-muted">{h(_when(d["decided_at"]))}</p></li>'
            for d in shown
        )
        + "</ol>"
        if shown
        else ""
    )
    facts = (
        '<dl class="abi-facts">'
        f"<div><dt>Events this week</dt><dd>{int(row['row_count']):,}</dd></div>"
        f"<div><dt>Severity / environment</dt><dd>{h(row.get('severity') or '—')} / {h(row.get('environment') or '—')}</dd></div>"
        f"<div><dt>First seen</dt><dd>{h(_when(row.get('first_seen')))}</dd></div>"
        f"<div><dt>Last seen</dt><dd>{h(_when(row.get('last_seen')))}</dd></div></dl>"
    )
    back = team_url(team_id, run_id, "findings", page=page)
    body = (
        f'<a class="abi-back" href="{h(back)}">{icon("back")}{h(team_name)} · {h(_week(run))}</a>'
        f'<div><div class="abi-row abi-gap-s">{chip(kind, tone)}{chip(str(row["alert_schema"]))}</div>'
        f'<h1 class="abi-finding-title">{h(title)}</h1>'
        f'<p class="abi-sub">{h(row["application"])} · {h(row.get("component") or "—")} · {_plural(int(row["row_count"]), "event")} this week</p></div>'
        '<div class="abi-split"><div class="abi-split-main">'
        '<section class="abi-panel tight" aria-labelledby="message-title"><h2 id="message-title" class="abi-h3">Alert message</h2>'
        f'<p class="abi-message">{h(row.get("message") or "(no message)")}</p></section>'
        '<section class="abi-panel tight" aria-labelledby="why-title"><h2 id="why-title" class="abi-h3">Why it was flagged</h2>'
        f"<p>{h(why)}</p><h3>Suggested fix</h3><p>{h(fix)}</p>{facts}</section>"
        '<details class="abi-more"><summary>Evidence and machine assessment</summary><div class="abi-more-body">'
        f'<p class="abi-mono">{h(finding)} · {h(row["key_field"])}</p>'
        f'<p class="abi-small">{h(str(row["quality_state"]).replace("_", " "))} · {h(row.get("llm_confidence") or "")}</p>'
        f'<h3>Stored evidence</h3><pre class="abi-pre">{h(_json(row.get("findings_evidence")))}</pre>'
        f'<h3>Representative document</h3><pre class="abi-pre">{h(_json(row.get("representative_doc")))}</pre></div></details>'
        "</div>"
        '<aside class="abi-split-side abi-panel tight" aria-labelledby="decision-title"><h2 id="decision-title" class="abi-h3">Your decision</h2>'
        f'{decision}{history_html}<p class="abi-tiny abi-muted">Decisions are kept next to the finding. They never change it.</p></aside>'
        "</div>"
    )
    return layout(f"{title} · {team_name}", body + dialogs, flash, error)


def decision_dialog(
    run: Mapping[str, Any],
    row: Mapping[str, Any],
    finding: str,
    title: str,
    token: str,
    page: int,
) -> str:
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{h(v)}">'
        for k, v in (
            ("schema", str(row["alert_schema"])),
            ("application", str(row["application"])),
            ("key_field", str(row["key_field"])),
            ("finding", finding),
            ("page", str(page)),
        )
    )
    order = [state for state in ("confirmed", "dismissed", "pending") if state in DECISION_STATES]
    order += [state for state in DECISION_STATES if state not in order]
    choices = "".join(
        f'<label class="abi-choice"><input type="radio" name="state" value="{h(state)}"{" checked" if index == 0 else ""}>'
        f'<span><span class="abi-item-title">{h(state.capitalize())}</span>'
        f'<span class="abi-item-sub">{h(DECISION_HINTS.get(state, ""))}</span></span></label>'
        for index, state in enumerate(order)
    )
    content = (
        hidden + f'<div class="abi-context"><strong>{h(title)}</strong>'
        f'<span class="abi-small abi-muted">{h(row.get("message") or "(no message)")}</span></div>'
        f'<fieldset><legend>Decision</legend><div class="abi-choices">{choices}</div></fieldset>'
        '<div class="abi-field"><label for="decision-note">Reason</label>'
        '<textarea id="decision-note" name="note" required maxlength="2000" placeholder="Why did you decide this?"></textarea></div>'
    )
    return dialog(
        "record-decision",
        "Record decision",
        f"/runs/{quote(str(run['run_id']), safe='')}/decide",
        token,
        content,
        "Save decision",
        note="Saved next to the automated finding. It never changes it.",
    )


def _json(value: Any) -> str:
    try:
        return json.dumps(json.loads(str(value)), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        return str(value or "No stored evidence")


# ------------------------------------------------------------------ Decisions and Schedule


def decisions_page(
    rows: Sequence[Mapping[str, Any]],
    total: int,
    page: int,
    teams: Sequence[tuple[str, str, bool]],
    *,
    team: str = "",
    state: str = "",
    view: str = "recorded",
    counts: tuple[int, int] = (0, 0),
) -> str:
    """Findings of published weeks that need a decision, or the decisions recorded so far.

    ``counts`` is (findings needing a decision, recorded decisions) for the selected team.
    """
    needed = view == "needed"
    team_options = '<option value="">All teams</option>' + "".join(
        f'<option value="{h(key)}"{" selected" if key == team else ""}>{h(label)}</option>'
        for key, label, _ in teams
    )
    state_options = '<option value="">Any decision</option>' + "".join(
        f'<option value="{h(value)}"{" selected" if value == state else ""}>{h(value.capitalize())}</option>'
        for value in DECISION_STATES
    )
    filters = (
        '<form class="abi-filters" method="get" action="/decisions">'
        + ("" if needed else '<input type="hidden" name="view" value="recorded">')
        + f'<div class="abi-field"><label for="filter-team">Team</label><select id="filter-team" name="team">{team_options}</select></div>'
        + (
            ""
            if needed
            else f'<div class="abi-field"><label for="filter-state">Decision</label><select id="filter-state" name="state">{state_options}</select></div>'
        )
        + '<button type="submit" class="abi-btn">Show</button></form>'
    )
    by_team = {"team": team} if team else {}
    switch = (
        '<nav class="abi-filter" aria-label="Show decisions">'
        f'<a href="/decisions{h("?" + urlencode(by_team)) if team else ""}"{" aria-current=page" if needed else ""}>'
        f"Needs a decision · {counts[0]:,}</a>"
        f'<a href="/decisions?{h(urlencode({**by_team, "view": "recorded"}))}"{"" if needed else " aria-current=page"}>'
        f"Recorded · {counts[1]:,}</a></nav>"
    )
    items = []
    for row in rows:
        url = (
            "/runs/"
            + quote(str(row["run_id"]), safe="")
            + "/findings?"
            + urlencode({"finding": finding_key(row, str(row["finding_id"]))})
        )
        where = " · ".join(
            part
            for part in (
                str(row.get("team_display_name") or row["team_id"]),
                _week(row),
                str(row["application"]),
            )
            if part
        )
        if needed:
            title, _, _, kind, _ = _finding_text(row, str(row["finding_id"]))
            detail = f"{kind} · {row['alert_schema']} · {_plural(int(row['row_count']), 'event')}"
            items.append(
                f'<li><a class="abi-item" href="{h(url)}"><span>{chip("No decision", "none")}</span>'
                f'<span class="abi-item-main"><span class="abi-item-title">{h(title)}</span>'
                f'<span class="abi-item-sub">{h(where)}</span><span class="abi-item-note">{h(detail)}</span></span>'
                f'<span class="abi-item-when">Published {h(_when(row["published_at"]))}</span></a></li>'
            )
        else:
            items.append(
                f'<li><a class="abi-item" href="{h(url)}"><span>{badge(str(row["state"]))}</span>'
                f'<span class="abi-item-main"><span class="abi-item-title">{h(principle_title(str(row["finding_id"])))}</span>'
                f'<span class="abi-item-sub">{h(where)}</span><span class="abi-item-note">{h(row["note"])}</span></span>'
                f'<span class="abi-item-when">{h(_when(row["decided_at"]))}</span></a></li>'
            )
    if items:
        listing = (
            f'<section class="abi-panel flush"><ul class="abi-list">{"".join(items)}</ul></section>'
        )
    elif needed:
        message = "Nothing needs a decision for this team." if team else "Nothing needs a decision."
        listing = (
            f'<section class="abi-panel abi-empty"><h2>{message}</h2>'
            "<p>Every finding of a published week has a decision.</p></section>"
        )
    else:
        message = "No decisions match these filters." if team or state else "No decisions yet."
        listing = (
            f'<section class="abi-panel abi-empty"><h2>{message}</h2>'
            "<p>Decisions are recorded on the findings of published weeks.</p></section>"
        )
    query = {**by_team, **({} if needed else {"state": state, "view": "recorded"})}
    query = {key: value for key, value in query.items() if value}
    noun = "finding" if needed else "decision"
    pages = max(1, (total + 49) // 50)
    links = "".join(
        f'<a href="/decisions?{h(urlencode({**query, "page": target}))}">{label}</a>'
        for target, label in ((page - 1, "Previous"), (page + 1, "Next"))
        if 1 <= target <= pages
    )
    pagination = (
        f'<div class="abi-pager"><span>Page {page} of {pages} · {_plural(total, noun)}</span><span>{links}</span></div>'
        if total > 50
        else f'<p class="abi-small abi-muted">{_plural(total, noun)}</p>'
    )
    sub = (
        "Findings of published weeks with no decision yet, newest week first. "
        "Open one to record a decision."
        if needed
        else "What reviewers decided about findings. The automated findings never change."
    )
    body = (
        f'<div class="abi-head"><div><h1>Decisions</h1><p class="abi-sub">{sub}</p></div>{filters}</div>'
        f"{switch}{listing}{pagination}"
    )
    return layout("Decisions", body, nav="decisions")


SCHEDULE_NEXT = {
    "held": "Retries daily · publishes on its own three days after it was first held",
    "failed": "Retries daily",
    "stored": "Waits for the held week before it",
    "blocked": "Needs an operator: the latest published week is not on a Monday boundary",
    "expired": "Past retention · the next week publishes across the gap",
    "published": "Next week",
}


def schedule_page(
    teams: Sequence[tuple[str, str, bool]],
    overview: Mapping[str, Mapping[str, Any]],
    now: datetime,
) -> str:
    latest = latest_week_end(now)
    following = latest + WEEK
    enrolled = sum(1 for _, _, on in teams if on)
    rows = []
    for team_id, name, on in teams:
        last = overview.get(team_id, {}).get("last") or {}
        outcome = str(last.get("outcome") or "")
        week = (
            format_week(_utc(last["window_end"]) - WEEK, _utc(last["window_end"]))
            if last.get("window_end")
            else ""
        )
        if not on:
            shown, next_step = '<span class="abi-muted">Manual</span>', "Not on the weekly schedule"
        elif not outcome:
            shown, next_step = (
                '<span class="abi-muted">Not run yet</span>',
                "First review after the next Monday",
            )
        else:
            shown = badge(outcome)
            next_step = SCHEDULE_NEXT.get(outcome, "Next scheduled run")
        detail = (
            f'<span class="abi-item-sub">{h(last.get("detail"))}</span>'
            if last.get("detail")
            else ""
        )
        url = team_url(team_id, str(last["run_id"]) if last.get("run_id") else None)
        rows.append(
            f'<tr><td><a href="{h(url)}">{h(name)}</a></td><td>{shown}'
            f'<span class="abi-item-sub">{h(week)}{" · " + h(_when(last.get("invoked_at"))) if last.get("invoked_at") else ""}</span></td>'
            f'<td><span class="abi-small">{h(next_step)}</span>{detail}</td></tr>'
        )
    table = (
        '<div class="abi-table-wrap"><table class="abi-table"><caption>Latest outcome by team</caption>'
        '<thead><tr><th scope="col">Team</th><th scope="col">Outcome</th><th scope="col">What happens next</th></tr></thead>'
        f"<tbody>{''.join(rows) or '<tr><td colspan=3>No registered teams.</td></tr>'}</tbody></table></div>"
    )
    body = (
        '<div><h1>Schedule</h1><p class="abi-sub">Every team on the schedule is reviewed for each Monday-to-Monday '
        "week (UTC). Healthy weeks publish on their own.</p></div>"
        f'<section class="abi-panel tight abi-next" aria-label="Next week"><span class="abi-next-icon">{icon("schedule")}</span>'
        f'<div><span class="abi-small abi-muted">Next week to review</span><strong>{h(format_week(latest, following))}</strong></div>'
        f'<div class="abi-small">Reviewed for {_plural(enrolled, "team")} after it ends on {h(_day_month(following))} at 00:00 UTC. '
        "Held and failed weeks are retried every day.</div></section>"
        f"{table}"
        '<p class="abi-small abi-muted">A week still held after three days publishes anyway, with a note '
        "telling readers how many alerts the model could not assess.</p>"
    )
    return layout("Schedule", body, nav="schedule")
