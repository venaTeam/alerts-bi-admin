"""The admin app's server-rendered HTML.

Shares the portal's stylesheet and escaping. Every form posts back to this app and carries
the anti-forgery token; there is no script.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from typing import Any
from urllib.parse import quote, urlencode

from alerts_bi_operations.review.decisions import DECISION_STATES, findings_on
from alerts_bi_shared.ui.assets import STYLESHEET as PORTAL_STYLESHEET
from alerts_bi_shared.ui.explain import (
    format_instant,
    format_week,
    principle_title,
    rule_explanation,
)
from alerts_bi_shared.ui.html import h

__all__ = [
    "ADMIN_CSS",
    "STYLESHEET",
    "STYLESHEET_PATH",
    "dashboard_page",
    "error_page",
    "findings_page",
    "layout",
    "summary_link",
    "team_page",
]

ADMIN_CSS = """
.admin-bar{background:var(--ink);color:var(--surface)}
.admin-bar a,.admin-bar .brand{color:var(--surface)}
.admin-bar .ro{color:var(--surface);border-color:var(--surface)}
table.runs{width:100%;border-collapse:collapse;min-width:900px;font-size:13px}
table.runs th{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);font-weight:600;text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
table.runs td{padding:10px;border-bottom:1px solid var(--line);vertical-align:top}
.flash{padding:10px 14px;border-radius:8px;background:var(--good-soft);color:var(--ink)}
.flash.error{background:var(--rule-soft)}
form.inline{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:6px}
form.inline input[type=text],form.inline select{font:inherit;font-size:12.5px;padding:4px 8px;border:1px solid var(--line-strong);border-radius:6px;background:var(--surface);color:var(--ink);min-width:0}
form.inline input[type=text]{flex:1 1 220px}
form.inline label{font-size:12px;color:var(--ink-2);display:inline-flex;gap:4px;align-items:center}
.state{font-size:12px;font-weight:600}
.state.published{color:var(--good)} .state.held{color:var(--rule)} .state.none{color:var(--muted)}
details.act summary{cursor:pointer;font-size:12.5px;color:var(--focus)}
.picker{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.picker select{font:inherit;font-size:13.5px;color:var(--ink);background:var(--surface);border:1px solid var(--line-strong);border-radius:6px;padding:6px 10px;max-width:100%}
.runstrip{background:var(--surface-2);border:1px solid var(--line);border-radius:8px;padding:8px 12px;font-size:12.5px}
p.links{margin:0;font-size:13px}
ul.panels{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:12px}
pre.sql{margin:4px 0 0;padding:10px 12px;background:var(--surface-2);border:1px solid var(--line);border-radius:6px;font-family:var(--mono);font-size:12.5px;white-space:pre-wrap;overflow-wrap:anywhere}
mark.sup{background:var(--rule-soft);color:var(--rule);border-radius:3px;padding:0 2px}
mark.sup.unmeasured{background:var(--ready-soft);color:var(--ready)}
""".strip()


STYLESHEET = PORTAL_STYLESHEET + "\n" + ADMIN_CSS
#: Content-addressed, so a changed stylesheet is never served from a stale cache.
STYLESHEET_PATH = f"/assets/admin-{sha256(STYLESHEET.encode()).hexdigest()[:12]}.css"


def layout(title: str, user: str, body: str, flash: str | None = None, error: bool = False) -> str:
    banner = f'<p class="flash{" error" if error else ""}">{h(flash)}</p>' if flash else ""
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{h(title)} · Alerts BI admin</title>"
        f'<link rel="stylesheet" href="{STYLESHEET_PATH}">'
        "</head><body>"
        '<header class="topbar admin-bar"><div class="wrap">'
        '<a class="brand" href="/"><span class="brand-mark" aria-hidden="true"></span>'
        "Alerts BI <small>· Admin</small></a>"
        f'<span class="ro">Signed in as {h(user)}</span>'
        "</div></header>"
        f'<main class="wrap">{banner}{body}</main>'
        "</body></html>\n"
    )


def error_page(status: int, message: str, user: str = "") -> str:
    return layout(
        str(status),
        user or "nobody",
        f'<section class="intro"><div class="eyebrow">{status}</div><h1>{h(message)}</h1>'
        '<p><a href="/">Back to all teams</a></p></section>',
    )


def _team_url(team_id: str) -> str:
    return "/teams/" + quote(team_id, safe="")


def summary_link(team_id: str, run_id: str | None = None) -> str:
    """The Summary page of a team, for one run or (without ``run_id``) its latest."""
    path = _team_url(team_id) + "/summary"
    return path + ("?" + urlencode({"run_id": run_id}) if run_id else "")


def _when(value: Any) -> str:
    return format_instant(value) if isinstance(value, datetime) else "—"


def dashboard_page(
    user: str,
    teams: Sequence[tuple[str, str, bool]],
    overview: Mapping[str, Mapping[str, Any]],
    flash: str | None,
    error: bool,
) -> str:
    rows = []
    for team_id, name, enrolled in teams:
        info = overview.get(team_id, {})
        last = info.get("last")
        outcome = (
            f'<span class="state {"published" if last["outcome"] == "published" else "held"}">'
            f"{h(last['outcome'])}</span> {h(_when(last['invoked_at']))}"
            + (f'<div class="sub">{h(last["detail"])}</div>' if last["detail"] else "")
            if last
            else '<span class="state none">never scheduled</span>'
        )
        rows.append(
            "<tr>"
            f'<td><a class="team" href="{h(_team_url(team_id))}">{h(name)}</a>'
            f'<div class="sub mono">{h(team_id)}</div>'
            f'<div class="sub"><a href="{h(summary_link(team_id))}">Summary</a></div></td>'
            f"<td>{'weekly' if enrolled else 'manual only'}</td>"
            f"<td>{h(_when(info.get('latest')))}</td>"
            f'<td class="num">{info.get("weeks", 0)}</td>'
            f"<td>{outcome}</td>"
            "</tr>"
        )
    body = (
        '<section class="intro"><div class="eyebrow">Operator view</div><h1>Teams</h1>'
        "<p>Every registered team, whether it is on the weekly schedule, its latest published "
        "week and what the schedule last did. Open a team to publish or withdraw weeks.</p>"
        "</section>"
        '<section class="card table-wrap"><table class="dir"><thead><tr>'
        "<th>Team</th><th>Schedule</th><th>Latest published week ends</th><th>Weeks</th>"
        "<th>Last schedule outcome</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></section>"
    )
    return layout("Teams", user, body, flash, error)


def _hidden(token: str) -> str:
    return f'<input type="hidden" name="csrf" value="{h(token)}">'


def team_page(
    user: str,
    team_id: str,
    name: str,
    runs: Sequence[Mapping[str, Any]],
    log: Sequence[Mapping[str, Any]],
    token: str,
    flash: str | None,
    error: bool,
) -> str:
    rows = []
    for run in runs:
        run_id = str(run["run_id"])
        base = f"/runs/{quote(run_id, safe='')}"
        week = format_week(run["window_start"], run["window_end"])
        if run["publication_id"] is not None:
            state = (
                f'<span class="state published">published</span> {h(_when(run["published_at"]))} '
                f"by {h(run['published_by'])}"
                + (f'<div class="sub">{h(run["review_note"])}</div>' if run["review_note"] else "")
            )
            action = (
                '<details class="act"><summary>Withdraw</summary>'
                f'<form class="inline" method="post" action="{h(base)}/withdraw">{_hidden(token)}'
                '<input type="text" name="reason" required maxlength="1000" '
                'placeholder="Why readers should no longer see this week">'
                '<button class="button" type="submit">Withdraw</button></form></details>'
                f'<a href="{h(base)}/findings">Findings and decisions</a>'
            )
        elif run["status"] == "completed":
            withdrawn = " (withdrawn earlier)" if run["withdrawn"] else ""
            state = f'<span class="state none">not published{withdrawn}</span>'
            action = (
                '<details class="act"><summary>Publish</summary>'
                f'<form class="inline" method="post" action="{h(base)}/publish">{_hidden(token)}'
                '<input type="text" name="note" maxlength="4000" '
                'placeholder="Optional note shown to readers">'
                '<label><input type="checkbox" name="replace" value="1">replace this week</label>'
                '<label><input type="checkbox" name="allow_gap" value="1">allow a gap</label>'
                '<button class="button" type="submit">Publish</button></form></details>'
            )
        else:
            state = f'<span class="state held">{h(run["status"])}</span>'
            action = ""
        summary = (
            f'<a href="{h(summary_link(team_id, run_id))}">Summary</a>'
            if run["status"] == "completed"
            else ""
        )
        unassessed = int(run["unassessed"] or 0)
        model = h(run["model_version"] or "off") + (
            f'<div class="sub">{unassessed} not assessed</div>' if unassessed else ""
        )
        rows.append(
            "<tr>"
            f"<td>{h(week)}<div class='sub'>run_at {h(_when(run['run_at']))}</div></td>"
            f'<td class="mono">{h(run_id[:16])}</td>'
            f"<td>{model}</td>"
            f"<td>{state}</td>"
            f'<td><a href="{h(base)}/scorecard">Scorecard</a> {summary} {action}</td>'
            "</tr>"
        )
    log_rows = "".join(
        "<tr>"
        f"<td>{h(_when(entry['invoked_at']))}</td>"
        f"<td>{h(_when(entry['window_end']))}</td>"
        f"<td>{h(entry['outcome'])}</td>"
        f"<td>{h(entry['detail'] or '')}</td>"
        "</tr>"
        for entry in log
    )
    body = (
        f'<nav class="crumbs"><a href="/">All teams</a><span>/</span><span>{h(name)}</span></nav>'
        f'<section class="intro"><div class="eyebrow">Runs</div><h1>{h(name)}</h1></section>'
        '<section class="card table-wrap"><table class="runs"><thead><tr>'
        "<th>Week</th><th>Run</th><th>Model</th><th>Publication</th><th>Actions</th>"
        f"</tr></thead><tbody>{''.join(rows) or '<tr><td colspan=5>No runs yet.</td></tr>'}"
        "</tbody></table></section>"
        '<section><div class="section-h"><h2>Schedule log</h2></div>'
        '<div class="card table-wrap"><table class="runs"><thead><tr>'
        "<th>When</th><th>Week ending</th><th>Outcome</th><th>Detail</th></tr></thead>"
        f"<tbody>{log_rows or '<tr><td colspan=4>The schedule has not run for this team.</td></tr>'}"
        "</tbody></table></div></section>"
    )
    return layout(name, user, body, flash, error)


def findings_page(
    user: str,
    run: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    total: int,
    page: int,
    page_size: int,
    history: Mapping[tuple[str, str, str], Sequence[Mapping[str, Any]]],
    token: str,
    flash: str | None,
    error: bool,
) -> str:
    run_id = str(run["run_id"])
    base = f"/runs/{quote(run_id, safe='')}"
    items = []
    for row in rows:
        identity = (str(row["alert_schema"]), str(row["application"]), str(row["key_field"]))
        ids = findings_on(dict(row))
        core = [part for part in str(row["core_rule_ids"] or "").split(",") if part]
        readiness = [part for part in str(row["readiness_rule_ids"] or "").split(",") if part]
        labels = [
            *(f"{rule} · {rule_explanation(rule, None).title}" for rule in core),
            *(f"{rule} · {rule_explanation(rule, None).title} (readiness)" for rule in readiness),
            *(
                f"{finding} · {principle_title(finding)} (automated)"
                for finding in ids[len(core) + len(readiness) :]
            ),
        ]
        options = "".join(
            f'<option value="{h(f)}">{h(label)}</option>'
            for f, label in zip(ids, labels, strict=True)
        )
        states = "".join(f'<option value="{h(s)}">{h(s)}</option>' for s in DECISION_STATES)
        past = "".join(
            "<li>"
            f'<span class="chip human {h(d["state"])}">{h(str(d["state"]).capitalize())}</span>'
            f'<span class="text">{h(d["finding_id"])}: {h(d["note"])}</span>'
            f'<span class="when">{h(_when(d["decided_at"]))} · {h(d["decided_by"])}</span>'
            "</li>"
            for d in history.get(identity, [])
        )
        items.append(
            '<li class="card finding">'
            f"<h3>“{h(row['message'] or '(no message)')}”</h3>"
            f'<p class="sub">{h(row["application"])} &rsaquo; {h(row["component"] or "—")} · '
            f"{h(row['alert_schema'])} · {int(row['row_count']):,} firings · "
            f'<span class="mono">{h(row["key_field"])}</span></p>'
            f'<p class="sub">Findings: {h(", ".join(labels))}</p>'
            + (f'<div class="history"><ol>{past}</ol></div>' if past else "")
            + f'<form class="inline" method="post" action="{h(base)}/decide">{_hidden(token)}'
            f'<input type="hidden" name="schema" value="{h(row["alert_schema"])}">'
            f'<input type="hidden" name="application" value="{h(row["application"])}">'
            f'<input type="hidden" name="key_field" value="{h(row["key_field"])}">'
            f'<select name="finding" aria-label="Finding">{options}</select>'
            f'<select name="state" aria-label="Decision">{states}</select>'
            '<input type="text" name="note" required maxlength="2000" placeholder="Why">'
            '<button class="button" type="submit">Record decision</button></form>'
            "</li>"
        )
    pages = max(1, -(-total // page_size))
    nav = []
    if page > 1:
        nav.append(
            f'<a class="button" href="{h(base)}/findings?{urlencode({"page": page - 1})}">Previous</a>'
        )
    if page < pages:
        nav.append(
            f'<a class="button" href="{h(base)}/findings?{urlencode({"page": page + 1})}">Next</a>'
        )
    week = format_week(run["window_start"], run["window_end"])
    team_id = str(run["team_id"])
    body = (
        f'<nav class="crumbs"><a href="/">All teams</a><span>/</span>'
        f'<a href="{h(_team_url(team_id))}">{h(run["team_display_name"])}</a><span>/</span>'
        f"<span>Week of {h(week)}</span></nav>"
        f'<section class="intro"><div class="eyebrow">Findings and decisions</div>'
        f"<h1>{h(run['team_display_name'])} · week of {h(week)}</h1>"
        f"<p>{total:,} alerts carry a finding. A decision sits beside the machine's finding and "
        "never changes it; readers see the decision history.</p></section>"
        f'<ul class="wl">{"".join(items) or "<li class=empty>No findings this week.</li>"}</ul>'
        f'<div class="pager"><span>Page {page} of {pages}</span><span class="links">{"".join(nav)}</span></div>'
    )
    return layout(f"Findings · {run['team_display_name']}", user, body, flash, error)
