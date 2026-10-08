"""The approved operator workspace, rendered from SQL with authenticated POST actions."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from urllib.parse import quote, urlencode

from alerts_bi_operations.review.decisions import DECISION_STATES, findings_on
from alerts_bi_shared.ui.assets import STYLESHEET as SHARED_CSS
from alerts_bi_shared.ui.explain import (
    format_instant,
    format_week,
    principle_next_step,
    principle_title,
    rule_explanation,
)
from alerts_bi_shared.ui.html import h

from .assets import MOCK_CSS

EXTRA_CSS = """
html,body{margin:0;min-height:100%;color-scheme:light}body{background:#eef1f4}
#abi-console{border:0;border-radius:0;min-height:100vh}#abi-console .abi-shell{min-height:calc(100vh - 60px)}
#abi-console a{color:inherit;text-decoration:none}#abi-console a.abi-link{color:var(--abi-focus)}
#abi-console .abi-user{margin-right:0}#abi-console .abi-tab[aria-current=page]{color:var(--abi-ink);border-bottom-color:var(--abi-ink);font-weight:600}
#abi-console .abi-finding[aria-current=true]{background:var(--abi-selected);box-shadow:inset 3px 0 0 var(--abi-focus)}
#abi-console :is(a,button,input,textarea,select,summary):focus-visible{outline:3px solid var(--abi-focus);outline-offset:3px}
#abi-console button,#abi-console summary{cursor:pointer}#abi-console .abi-spacer{margin-left:auto}#abi-console .abi-mt{margin-top:8px}
#abi-console .abi-mb{margin-bottom:12px}#abi-console .abi-section{display:flex;flex-direction:column;gap:16px;min-width:0}
#abi-console .abi-pre{white-space:pre-wrap;overflow-wrap:anywhere;font:11px/1.6 ui-monospace,monospace;background:var(--abi-soft);border-radius:6px;padding:12px;max-height:320px;overflow:auto}
#abi-console .abi-detail-head,#abi-console .abi-evidence,#abi-console .abi-history{overflow-wrap:anywhere}
#abi-console .abi-finding strong{overflow-wrap:anywhere}#abi-console .abi-review-list{margin:12px 0;padding-left:18px;font-size:12px;display:grid;gap:8px}
#abi-console .abi-dialog{margin:auto;max-height:calc(100vh - 32px)}#abi-console .abi-check{display:flex;font-weight:400}
#abi-console .abi-check input{width:auto}#abi-console .abi-dialog h2{font-size:17px}#abi-console .abi-dialog-content form{display:contents}
#abi-console .abi-legacy{display:block;min-width:0;overflow:hidden}#abi-console .abi-legacy .sl-scroll{width:100%;max-width:100%;margin-left:0}
#abi-console .abi-legacy :is(.section-h,.block,.tools,.pager){padding:16px}#abi-console .abi-legacy :is(.links,.inline){display:flex;flex-wrap:wrap;gap:8px}
#abi-console .abi-legacy .button{display:inline-flex;padding:8px 12px;border:1px solid var(--abi-line);border-radius:6px;background:var(--abi-surface);font-size:12px}
#abi-console .abi-legacy .card{background:var(--abi-surface);border:1px solid var(--abi-line);border-radius:8px;margin-top:16px}
#abi-console .abi-legacy .table-wrap{overflow:auto}#abi-console .abi-legacy .sub{font-size:12px;color:var(--abi-muted)}
#abi-console .abi-legacy mark.sup{background:var(--abi-rule-soft);color:var(--abi-rule)}#abi-console .abi-legacy pre.sql{white-space:pre-wrap;overflow-wrap:anywhere}
#abi-console .abi-legacy .panels{list-style:none;padding:0}#abi-console .abi-legacy .panels li{margin:12px 0}
@media(min-width:1450px){#abi-console .abi-main{padding:28px 36px}}
"""
STYLESHEET = SHARED_CSS + "\n" + MOCK_CSS + "\n" + EXTRA_CSS
STYLESHEET_PATH = f"/assets/console-{sha256(STYLESHEET.encode()).hexdigest()[:12]}.css"
SCRIPT = """(() => {
let opener;
document.addEventListener('click', event => {
 const open = event.target.closest('[data-dialog]');
 if (open) { opener = open; document.getElementById(open.dataset.dialog).showModal(); }
 const close = event.target.closest('[data-close]');
 if (close) close.closest('dialog').close();
});
document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('close', () => opener?.focus()));
document.querySelectorAll('[data-team-selector]').forEach(select => {
 const choose = () => { select.form.action = '/teams/' + encodeURIComponent(select.value) + '/trigger'; };
 choose(); select.addEventListener('change', choose);
});
function windowLabel(input) {
 const label = input.closest('form').querySelector('[data-window]');
 const end = new Date(input.value + 'Z');
 if (!label || !Number.isFinite(end.getTime())) return;
 const start = new Date(end.getTime() - 168 * 3600000);
 const fmt = d => d.toISOString().slice(0,16).replace('T',' ');
 label.textContent = fmt(start) + ' → ' + fmt(end) + ' UTC · 168 hours';
}
document.querySelectorAll('[name=run_at]').forEach(input => {
 windowLabel(input); input.addEventListener('input', () => windowLabel(input));
});
document.addEventListener('submit', event => {
 const form = event.target;
 if (!form.matches('[data-trigger]')) return;
 const button = form.querySelector('button[type=submit]');
 button.disabled = true; button.textContent = 'Running analysis…';
 form.querySelector('[role=status]').textContent = 'Analysis is running. Keep this page open; the completed run will open here.';
});
window.addEventListener('pageshow', () => document.querySelectorAll('[data-trigger] button[type=submit]').forEach(b => {
 b.disabled = false; b.textContent = 'Start run';
}));
})();"""
SCRIPT_PATH = f"/assets/console-{sha256(SCRIPT.encode()).hexdigest()[:12]}.js"


def icon(name: str) -> str:
    paths = {
        "teams": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m20 0v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/><circle cx="9" cy="7" r="4"/>',
        "schedule": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18m-9 3v3l2 1"/>',
        "decisions": '<path d="m3 6 2 2 4-4m-6 12 2 2 4-4m4-8h8m-8 10h8"/>',
        "arrow": '<path d="M5 12h14m-6-6 6 6-6 6"/>',
        "back": '<path d="M19 12H5m6-6-6 6 6 6"/>',
        "plus": '<path d="M12 5v14M5 12h14"/>',
        "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v1"/>',
        "close": '<path d="m6 6 12 12M6 18 18 6"/>',
    }
    return f'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{paths.get(name, paths["info"])}</svg>'


def layout(
    title: str,
    user: str,
    body: str,
    flash: str | None = None,
    error: bool = False,
    nav: str = "teams",
) -> str:
    navigation = "".join(
        f'<a class="abi-nav" href="{url}"{chr(32) + "aria-current=page" if key == nav else ""}>{icon(key)}{label}</a>'
        for key, url, label in (
            ("teams", "/", "Teams"),
            ("schedule", "/schedule", "Schedule"),
            ("decisions", "/decisions", "Decisions"),
        )
    )
    banner = (
        f'<div role="status" class="abi-banner {"amber" if error else "good"}">{icon("info")}{h(flash)}</div>'
        if flash
        else ""
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{h(title)} · Alerts BI</title><link rel="stylesheet" href="{STYLESHEET_PATH}"><script src="{SCRIPT_PATH}" defer></script>'
        '</head><body><div id="abi-console" aria-label="Unified Alerts BI operator console">'
        '<header class="abi-top"><a class="abi-brand" href="/"><span class="abi-mark" aria-hidden="true"></span>Alerts BI '
        '<span class="abi-small abi-muted abi-brand-sub">/ Operations</span></a>'
        f'<div class="abi-user"><span class="abi-avatar">{h(user[:2].upper())}</span><span>Signed in as {h(user)}</span></div></header>'
        '<div class="abi-shell"><nav class="abi-sidebar" aria-label="Workspace navigation"><div class="abi-eyebrow">Workspace</div>'
        f'{navigation}<div class="abi-sidebar-foot">{icon("info")}<span>Operator workspace<br>Actions are audited</span></div></nav>'
        f'<main class="abi-main" id="abi-main">{banner}{body}</main></div></div></body></html>'
    )


def error_page(status: int, message: str, user: str = "") -> str:
    return layout(
        str(status),
        user,
        f'<h1>{status}</h1><p>{h(message)}</p><a class="abi-link" href="/">Back to teams</a>',
    )


def team_url(team_id: str, run_id: str | None = None, tab: str = "overview", **query: Any) -> str:
    params = {"tab": tab, **query}
    if run_id:
        params["run_id"] = run_id
    return "/teams/" + quote(team_id, safe="") + "?" + urlencode(params)


def summary_link(team_id: str, run_id: str | None = None) -> str:
    return team_url(team_id, run_id)


def _when(value: Any) -> str:
    return format_instant(value) if isinstance(value, datetime) else "—"


def _hidden(token: str) -> str:
    return f'<input type="hidden" name="csrf" value="{h(token)}">'


def publication_state(run: Mapping[str, Any]) -> str:
    return (
        "published"
        if run.get("publication_id")
        else "withdrawn"
        if run.get("withdrawn")
        else "unpublished"
    )


def badge(state: str, tone: str = "") -> str:
    if not tone:
        tone = {
            "completed": "good",
            "published": "good",
            "confirmed": "good",
            "failed": "rule",
            "withdrawn": "amber",
            "pending": "amber",
            "held": "amber",
            "v1": "v1",
            "v2": "v2",
        }.get(state, "")
    label = state if state in ("v1", "v2") else state.replace("_", " ").capitalize()
    return f'<span class="abi-badge {h(tone)}">{h(label)}</span>'


def _week(run: Mapping[str, Any]) -> str:
    return format_week(run["window_start"], run["window_end"])


def _source(run: Mapping[str, Any]) -> str:
    return "Weekly schedule" if run.get("scheduled") else "Manual / API"


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
        latest = info.get("run")
        latest_html = (
            f"{badge(str(latest['status']))}<small>{h(_week(latest))}</small>"
            if latest
            else '<span class="abi-muted">No runs yet</span>'
        )
        if latest and latest.get("unassessed"):
            latest_html += f"<small>{int(latest['unassessed']):,} unassessed</small>"
        published = (
            format_week(info["latest"] - timedelta(days=7), info["latest"])
            if info.get("latest")
            else "Not published yet"
        )
        url = h(team_url(team_id))
        rows.append(
            f'<tr><td><a class="abi-link" href="{url}">{h(name)}</a><small>{"Weekly schedule" if enrolled else "Manual only"} · {int(info.get("run_count", 0))} runs</small></td><td data-label="Latest run:">{latest_html}</td><td data-label="Published:">{h(published)}</td><td><a class="abi-link" href="{url}" aria-label="Open {h(name)}">Open team {icon("arrow")}</a></td></tr>'
        )
    body = (
        '<div class="abi-crumb">Operations <span>&rsaquo;</span> Teams</div><div class="abi-heading"><div><h1>Teams</h1>'
        '<p class="abi-subtitle">Open a team to manage its runs, review findings and publish results.</p></div>'
        f'<span class="abi-badge">{len(teams)} teams</span></div><section class="abi-panel abi-team-directory">'
        '<table class="abi-table"><thead><tr><th>Team / schedule</th><th>Latest run</th><th>Latest published week</th><th></th></tr></thead>'
        f"<tbody>{''.join(rows) or '<tr><td colspan=4>No registered teams.</td></tr>'}</tbody></table></section>"
        f'<div class="abi-banner">{icon("info")}Each team has one workspace for its run history, analysis, findings, decisions and publication.</div>'
    )
    return layout("Teams", user, body, flash, error)


def dialog(
    dialog_id: str,
    title: str,
    action: str,
    token: str,
    content: str,
    submit: str,
    *,
    danger: bool = False,
    trigger: bool = False,
) -> str:
    return (
        f'<dialog class="abi-dialog" id="{dialog_id}" aria-labelledby="{dialog_id}-title"><form method="post" action="{h(action)}"{" data-trigger" if trigger else ""}>'
        f'{_hidden(token)}<div class="abi-dialog-header"><h2 id="{dialog_id}-title">{h(title)}</h2><button type="button" class="abi-link" data-close aria-label="Close dialog">{icon("close")}</button></div>'
        f'<div class="abi-dialog-content">{content}</div><div class="abi-dialog-footer"><button type="button" class="abi-btn" data-close>Cancel</button>'
        f'<button type="submit" class="abi-btn {"abi-danger" if danger else "abi-primary"}">{h(submit)}</button></div></form></dialog>'
    )


def trigger_form(
    team_id: str, token: str, name: str = "", teams: Sequence[tuple[str, str, bool]] = ()
) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
    team_field = f"<div><label>Team</label><p>{h(name or team_id)}</p></div>"
    if teams:
        options = "".join(
            f'<option value="{h(key)}">{h(label)}</option>' for key, label, _ in teams
        )
        team_field = f'<div><label for="trigger-team">Team</label><select id="trigger-team" name="team" data-team-selector>{options}</select></div>'
    content = (
        f'{team_field}<div><label for="run-at">Window ends at · UTC</label><input id="run-at" name="run_at" type="datetime-local" value="{now}" required></div>'
        '<div class="abi-evidence-box"><strong>Analysis window</strong><span data-window>Exactly the preceding 168 hours</span></div>'
        '<div><label for="model-mode">Model mode</label><select id="model-mode" name="llm"><option value="live">Live · configured model</option><option value="fake">Fake · deterministic test model</option><option value="off">Off · skip assessment</option></select></div>'
        '<p class="abi-small abi-muted">The completed run stays unpublished until you publish it.</p><p role="status" aria-live="polite" class="abi-small"></p>'
    )
    return dialog(
        "new-run",
        "New run",
        f"/teams/{quote(team_id, safe='')}/trigger",
        token,
        content,
        "Start run",
        trigger=True,
    )


def publication_controls(run: Mapping[str, Any], token: str) -> tuple[str, str]:
    base = "/runs/" + quote(str(run["run_id"]), safe="")
    context = f'<div class="abi-evidence-box"><strong>{h(run.get("team_display_name") or run["team_id"])}</strong>{h(_week(run))}</div>'
    if run.get("publication_id"):
        content = (
            context
            + '<div><label for="withdraw-reason">Withdrawal reason</label><textarea id="withdraw-reason" name="reason" required maxlength="1000" placeholder="Why should this week be withdrawn?"></textarea></div><p class="abi-small abi-muted">Removes this week from the reader portal and retains its audit history.</p>'
        )
        return (
            '<button class="abi-btn abi-danger" type="button" data-dialog="withdraw-review">Withdraw review</button>',
            dialog(
                "withdraw-review",
                "Withdraw review",
                base + "/withdraw",
                token,
                content,
                "Withdraw review",
                danger=True,
            ),
        )
    if run["status"] != "completed":
        return (
            '<button type="button" class="abi-btn abi-primary" data-dialog="new-run">Retry run</button>',
            "",
        )
    content = (
        context
        + '<div><label for="review-note">Review note · optional</label><textarea id="review-note" name="note" maxlength="4000" placeholder="Add context for this publication"></textarea></div>'
        '<label class="abi-check"><input type="checkbox" name="replace" value="1">Replace the published run for this exact week</label>'
        '<label class="abi-check"><input type="checkbox" name="allow_gap" value="1">Allow a gap in published weeks</label>'
        '<p class="abi-small abi-muted">Publishes this 168-hour window to the reader portal. Replacing a week withdraws its previous publication.</p>'
    )
    return (
        '<button class="abi-btn abi-primary" type="button" data-dialog="publish-review">Publish review</button>',
        dialog(
            "publish-review", "Publish review", base + "/publish", token, content, "Publish review"
        ),
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
        f'<a class="abi-link" href="{h(team_url(team_id, run_id, tab, **{param: target}))}">{label}</a>'
        for target, label in ((page - 1, "Previous"), (page + 1, "Next"))
        if 1 <= target <= count
    )
    return f'<div class="abi-row abi-between abi-panel-pad abi-small"><span>Page {page} of {count} · {total:,} total</span><span class="abi-row">{links}</span></div>'


def team_page(
    user: str,
    team_id: str,
    name: str,
    enrolled: bool,
    runs: Sequence[Mapping[str, Any]],
    total: int,
    run_page: int,
    selected: Mapping[str, Any] | None,
    tab: str,
    content: str,
    token: str,
    flash: str | None,
    error: bool,
) -> str:
    run_id = str(selected["run_id"]) if selected else None
    rows = []
    for row in runs:
        active = str(row["run_id"]) == run_id
        url = h(team_url(team_id, str(row["run_id"]), tab, run_page=run_page)) + "#selected-run"
        pick = (
            badge("selected")
            if active
            else f'<a class="abi-link" href="{url}">Select run {icon("arrow")}</a>'
        )
        rows.append(
            f'<tr aria-selected="{str(active).lower()}"><td><a class="abi-link" href="{url}">{h(_week(row))}</a><small><span class="abi-mono">{h(str(row["run_id"])[:12])}</span> · {h(_source(row))}</small></td><td data-label="Analysis:">{badge(str(row["status"]))}</td><td data-label="Publication:">{badge(publication_state(row))}</td><td>{pick}</td></tr>'
        )
    body = (
        f'<div class="abi-row abi-between"><a class="abi-link" href="/">{icon("back")}All teams</a><div class="abi-crumb">Operations / Teams</div></div>'
        f'<div class="abi-heading"><div><div class="abi-eyebrow">Team workspace</div><h1>{h(name)}</h1><div class="abi-team-context"><span>{h(str(selected.get("sources_label", "Team sources"))) if selected else "Team sources"}</span><span>{"Weekly · Monday to Monday UTC" if enrolled else "Manual runs"}</span></div></div><button class="abi-btn abi-primary" type="button" data-dialog="new-run">{icon("plus")}New run</button></div>'
        f'<section class="abi-panel abi-team-runs"><div class="abi-list-head"><h3>Team runs <span class="abi-muted">· {total}</span></h3><span class="abi-small abi-muted">Select a run to review below</span></div><table class="abi-table"><thead><tr><th>Reporting week / run</th><th>Analysis</th><th>Publication</th><th></th></tr></thead><tbody>{"".join(rows) or "<tr><td colspan=4>No runs yet. Start the first run above.</td></tr>"}</tbody></table>{pager(team_id, run_id, tab, run_page, total, 20, "run_page")}</section>'
    )
    dialogs = trigger_form(team_id, token, name)
    if selected:
        controls, publication_dialog = publication_controls(selected, token)
        dialogs += publication_dialog
        state = publication_state(selected)
        body += (
            '<section id="selected-run" class="abi-section"><div class="abi-selected-run"><div><div class="abi-eyebrow">Selected run</div>'
            f'<h2>{h(_week(selected))}</h2><p class="abi-subtitle">{h(_when(selected["window_start"]))} → {h(_when(selected["window_end"]))} · 168 hours · {h(_source(selected))}</p></div><div class="abi-row">{controls}</div></div>'
            f'<div class="abi-lifecycle"><span>Analysis {badge(str(selected["status"]))}</span><span>Publication {badge(state)}</span><span class="abi-mono abi-muted abi-spacer">{h(str(run_id)[:16])}</span></div>'
        )
        if state == "published":
            body += f'<div class="abi-banner good">{icon("info")}<span>Visible in the reader portal · Published by {h(selected.get("published_by"))} · {h(_when(selected.get("published_at")))}</span></div>'
        elif state == "withdrawn":
            body += f'<div class="abi-banner amber">{icon("info")}This review was withdrawn. Its findings and decision history remain available here.</div>'
        if selected.get("schedule_outcome") == "held" and state != "published":
            body += f'<div class="abi-banner amber">{icon("info")}<span>Publication held: {h(selected.get("schedule_detail") or "Assessment is incomplete.")} Retry is scheduled.</span></div>'
        body += '<nav class="abi-tabs" aria-label="Run details">'
        for value, label in (
            ("overview", "Overview"),
            ("findings", "Findings"),
            ("decisions", "Decisions"),
            ("activity", "Activity"),
        ):
            current = ' aria-current="page"' if value == tab else ""
            count = (
                f'<span class="abi-count">{int(selected.get(value + "_count", 0))}</span>'
                if value in ("findings", "decisions")
                else ""
            )
            body += f'<a class="abi-tab"{current} href="{h(team_url(team_id, run_id, value, run_page=run_page))}#selected-run">{label} {count}</a>'
        body += f'</nav>{content}<div class="abi-foot"><span class="abi-mono">Run {h(run_id)}</span><span>Human decisions preserve the machine assessment</span></div></section>'
    else:
        body += content
    return layout(name, user, body + dialogs, flash, error)


def _json(value: Any) -> str:
    try:
        return json.dumps(json.loads(str(value)), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        return str(value or "No stored evidence")


def finding_key(row: Mapping[str, Any], finding_id: str) -> str:
    identity = [str(row.get(k, "")) for k in ("alert_schema", "application", "key_field")]
    return sha256(json.dumps([*identity, finding_id]).encode()).hexdigest()[:24]


def _finding_text(row: Mapping[str, Any], finding_id: str) -> tuple[str, str, str, str, str]:
    if not finding_id.startswith("R"):
        return (
            principle_title(finding_id),
            str(row.get("llm_justification") or "No model explanation was stored."),
            principle_next_step(finding_id),
            "Needs review" if row.get("quality_state") == "needs_review" else "Model advisory",
            "model",
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
        "amber" if explanation.readiness else "rule",
    )


def findings_content(
    run: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    total: int,
    page: int,
    token: str,
    history: Mapping[tuple[str, str, str], Sequence[Mapping[str, Any]]],
    selected: str = "",
) -> str:
    choices = [(row, finding) for row in rows for finding in findings_on(dict(row))]
    if not choices:
        return (
            '<div class="abi-panel abi-empty"><h3>No findings on this page</h3><p class="abi-small abi-muted">This run has no findings to review here.</p></div>'
            + pager(str(run["team_id"]), str(run["run_id"]), "findings", page, total, 50)
        )
    active = next(
        ((row, finding) for row, finding in choices if finding_key(row, finding) == selected),
        choices[0],
    )
    links = []
    for row, finding in choices:
        title, _, _, kind, tone = _finding_text(row, finding)
        identity = (str(row["alert_schema"]), str(row["application"]), str(row["key_field"]))
        past = [d for d in history.get(identity, []) if d["finding_id"] == finding]
        current = ' aria-current="true"' if (row, finding) == active else ""
        url = team_url(
            str(run["team_id"]),
            str(run["run_id"]),
            "findings",
            page=page,
            finding=finding_key(row, finding),
        )
        links.append(
            f'<a class="abi-finding"{current} href="{h(url)}#finding-detail"><div class="abi-row abi-between">{badge(kind, tone)}{badge(str(row["alert_schema"]))}</div><strong>{h(title)}</strong><div class="abi-small">{h(row.get("component") or row["application"])} · {int(row["row_count"]):,} events</div><div class="abi-row abi-between abi-mt"><span class="abi-small">{h(str(past[-1]["state"]).capitalize()) if past else "No decision"}</span>{icon("arrow")}</div></a>'
        )
    row, finding = active
    title, why, fix, kind, tone = _finding_text(row, finding)
    identity = (str(row["alert_schema"]), str(row["application"]), str(row["key_field"]))
    past = [d for d in history.get(identity, []) if d["finding_id"] == finding]
    human = '<p class="abi-small abi-muted abi-mt">No human decision yet.</p>'
    if past:
        latest = past[-1]
        human = f'<div class="abi-row abi-mt">{badge(str(latest["state"]))}<span class="abi-small abi-muted">{h(latest["decided_by"])} · {h(_when(latest["decided_at"]))}</span></div><p class="abi-small abi-mt">{h(latest["note"])}</p>'
        human += (
            '<details class="abi-mt"><summary>Decision history for this finding</summary><ol class="abi-history">'
            + "".join(
                f'<li>{badge(str(d["state"]))}<p class="abi-small">{h(d["note"])}</p><span class="abi-small abi-muted">{h(d["decided_by"])} · {h(_when(d["decided_at"]))} · Run {h(str(d["run_id"])[:12])}</span></li>'
                for d in reversed(past)
            )
            + "</ol></details>"
        )
    decision_dialog = ""
    if run.get("publication_id"):
        human += '<button type="button" class="abi-btn" data-dialog="record-decision">Record decision</button>'
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
        options = "".join(f'<option value="{s}">{s.capitalize()}</option>' for s in DECISION_STATES)
        content = (
            hidden
            + f'<div class="abi-evidence-box"><strong>{h(title)}</strong>{h(row.get("message") or "(no message)")}</div><div><label for="decision-state">Decision</label><select id="decision-state" name="state">{options}</select></div><div><label for="decision-note">Reason</label><textarea id="decision-note" name="note" required maxlength="2000" placeholder="Explain your assessment"></textarea></div><p class="abi-small abi-muted">Adds an audit entry alongside the automated finding.</p>'
        )
        decision_dialog = dialog(
            "record-decision",
            "Record decision",
            f"/runs/{quote(str(run['run_id']), safe='')}/decide",
            token,
            content,
            "Record decision",
        )
    else:
        human += '<p class="abi-small abi-muted abi-mt">Publish this run before recording human decisions.</p>'
    detail = (
        f'<article class="abi-panel" id="finding-detail"><div class="abi-detail-head"><div class="abi-row abi-between">{badge(kind, tone)}{badge(str(row["alert_schema"]))}</div><h2>{h(title)}</h2><p class="abi-small">{h(row.get("message") or "(no message)")}</p><p class="abi-small abi-muted abi-mt">{h(row["application"])} · {h(row.get("component") or "—")}</p></div>'
        f'<div class="abi-evidence"><div class="abi-evidence-box"><strong>Why this was flagged</strong>{h(why)}</div><dl class="abi-dl"><div><dt>Events this week</dt><dd>{int(row["row_count"]):,}</dd></div><div><dt>Severity / environment</dt><dd>{h(row.get("severity") or "—")} / {h(row.get("environment") or "—")}</dd></div></dl><div><div class="abi-eyebrow abi-mb">Suggested change</div><p class="abi-small">{h(fix)}</p></div>'
        f'<details><summary>Finding identity and machine assessment</summary><p class="abi-mono">{h(finding)} · {h(row["key_field"])}</p><p class="abi-small">{h(str(row["quality_state"]).replace("_", " "))} · {h(row.get("llm_confidence") or "")}</p><h3 class="abi-mt">Stored evidence</h3><pre class="abi-pre">{h(_json(row.get("findings_evidence")))}</pre><h3>Representative document</h3><pre class="abi-pre">{h(_json(row.get("representative_doc")))}</pre></details></div><div class="abi-human"><h3>{icon("decisions")}Human decision</h3>{human}</div></article>'
    )
    return f'<div class="abi-row abi-between abi-small abi-muted"><span>{len(choices)} findings on this page · select one to inspect</span><span>Machine findings + human decisions</span></div><div class="abi-workspace"><section class="abi-panel"><div class="abi-list-head"><h3>Findings</h3><span class="abi-small abi-muted">{total:,} alerts</span></div>{"".join(links)}</section>{detail}</div>{pager(str(run["team_id"]), str(run["run_id"]), "findings", page, total, 50)}{decision_dialog}'


def _decision_items(rows: Sequence[Mapping[str, Any]], run: Mapping[str, Any] | None = None) -> str:
    items = []
    for row in rows:
        context = run or row
        url = (
            "/runs/"
            + quote(str(context["run_id"]), safe="")
            + "/findings?"
            + urlencode({"finding": finding_key(row, str(row["finding_id"]))})
        )
        items.append(
            f'<li><div class="abi-row abi-between">{badge(str(row["state"]))}<span class="abi-small abi-muted">{h(_when(row["decided_at"]))}</span></div><h3 class="abi-mt">{h(principle_title(str(row["finding_id"])))}</h3><p class="abi-small abi-mt">{h(row["note"])}</p><p class="abi-small abi-muted abi-mt">{h(row["decided_by"])} · {h(context.get("team_display_name") or context["team_id"])} · {h(_week(context))}</p><p class="abi-mono abi-muted">{h(row["alert_schema"])} · {h(row["application"])} · {h(row["key_field"])}</p><a class="abi-link abi-mt" href="{h(url)}">Open finding {icon("arrow")}</a></li>'
        )
    if not items:
        return '<div class="abi-empty"><h3>No human decisions yet</h3><p class="abi-small abi-muted">Review the evidence in Findings; record decisions after publication.</p></div>'
    return '<ol class="abi-history">' + "".join(items) + "</ol>"


def decisions_content(
    run: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], total: int, page: int
) -> str:
    return (
        '<section class="abi-panel abi-panel-pad"><h3>Decision history</h3><p class="abi-small abi-muted abi-mt">Human assessments for this run. Earlier decisions remain in the audit history.</p>'
        + _decision_items(rows, run)
        + "</section>"
        + pager(str(run["team_id"]), str(run["run_id"]), "decisions", page, total, 50)
    )


def _global_header(label: str, teams: Sequence[tuple[str, str, bool]]) -> str:
    trigger = (
        '<button type="button" class="abi-btn abi-primary" data-dialog="new-run">'
        + icon("plus")
        + "New run</button>"
        if teams
        else ""
    )
    return f'<div class="abi-row abi-between"><div class="abi-crumb">Operations <span>&rsaquo;</span> {label}</div>{trigger}</div>'


def decisions_page(
    user: str,
    rows: Sequence[Mapping[str, Any]],
    total: int,
    page: int,
    teams: Sequence[tuple[str, str, bool]],
    token: str,
) -> str:
    links = "".join(
        f'<a class="abi-link" href="/decisions?page={n}">{label}</a>'
        for n, label in ((page - 1, "Previous"), (page + 1, "Next"))
        if 1 <= n <= (total + 49) // 50
    )
    pagination = (
        f'<div class="abi-row abi-between abi-small"><span>Page {page} · {total} total</span><span class="abi-row">{links}</span></div>'
        if total > 50
        else ""
    )
    return layout(
        "Decisions",
        user,
        _global_header("Decisions", teams)
        + '<div class="abi-heading"><div><h1>Decisions</h1><p class="abi-subtitle">Human assessments across teams and runs. Machine findings remain unchanged.</p></div>'
        + f'<span class="abi-badge">{total} {"decision" if total == 1 else "decisions"}</span></div><section class="abi-panel abi-panel-pad">{_decision_items(rows)}</section>{pagination}<p class="abi-small abi-muted">Each decision belongs to one finding on one exact alert identity. Earlier decisions remain visible.</p>'
        + (trigger_form(teams[0][0], token, teams=teams) if teams else ""),
        nav="decisions",
    )


def schedule_page(
    user: str,
    teams: Sequence[tuple[str, str, bool]],
    overview: Mapping[str, Mapping[str, Any]],
    token: str,
) -> str:
    rows = []
    for team_id, name, enrolled in teams:
        last = overview.get(team_id, {}).get("last") or {}
        week = (
            format_week(last["window_end"] - timedelta(days=7), last["window_end"])
            if last.get("window_end")
            else "No covered week yet"
        )
        outcome = str(last.get("outcome") or ("Not run yet" if enrolled else "Manual only"))
        action = (
            "Daily retry while held"
            if outcome == "held"
            else "Next scheduled invocation"
            if enrolled
            else "Start a run from the team workspace"
        )
        rows.append(
            f'<tr><td><a class="abi-link" href="{h(team_url(team_id, str(last["run_id"]) if last.get("run_id") else None))}">{h(name)}</a><small>{h(week)}</small></td><td>{badge(outcome)}<small>{h(_when(last.get("invoked_at")))}</small><small>{h(last.get("detail") or "")}</small></td><td>{h(action)}</td></tr>'
        )
    return layout(
        "Schedule",
        user,
        _global_header("Schedule", teams)
        + '<div class="abi-heading"><div><h1>Weekly schedule</h1><p class="abi-subtitle">Monday 00:00 to Monday 00:00 UTC · daily retry</p></div></div><section class="abi-panel"><table class="abi-table"><thead><tr><th>Team / covered week</th><th>Latest outcome</th><th>Next action</th></tr></thead><tbody>'
        + "".join(rows)
        + f'</tbody></table></section><div class="abi-banner">{icon("info")}A held week is retried. Later weeks are stored until it is resolved; after three days, the held week is published with an assessment note.</div>'
        + (trigger_form(teams[0][0], token, teams=teams) if teams else ""),
        nav="schedule",
    )


def activity_content(
    logs: Sequence[Mapping[str, Any]],
    publications: Sequence[Mapping[str, Any]],
    run: Mapping[str, Any] | None = None,
) -> str:
    events: list[tuple[Any, str, str]] = []
    if run:
        events.append((run.get("started_at") or run["run_at"], "Analysis started", _source(run)))
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
                f"Published by {row['published_by']}",
                str(row.get("review_note") or "Visible in the reader portal."),
            )
        )
        if row.get("withdrawn_at"):
            events.append(
                (
                    row["withdrawn_at"],
                    f"Withdrawn by {row['withdrawn_by']}",
                    str(row.get("withdrawn_reason") or ""),
                )
            )
    items = "".join(
        f'<li><div class="abi-row abi-between"><strong>{h(title)}</strong><span class="abi-small abi-muted">{h(_when(at))}</span></div><p class="abi-small abi-muted abi-mt">{h(detail)}</p></li>'
        for at, title, detail in sorted(events, key=lambda item: item[0], reverse=True)
    )
    schedule = "".join(
        f'<li>{badge(str(row["outcome"]))}<span class="abi-small abi-muted"> {h(_when(row["invoked_at"]))}</span><p class="abi-small">{h(row.get("detail") or "")}</p></li>'
        for row in logs
    )
    return f'<section class="abi-panel abi-panel-pad"><h3>Run activity</h3><ol class="abi-history">{items or "<li>No activity yet.</li>"}</ol></section><details class="abi-panel abi-panel-pad"><summary>Team schedule log</summary><ol class="abi-history">{schedule or "<li>No schedule events yet.</li>"}</ol></details>'
