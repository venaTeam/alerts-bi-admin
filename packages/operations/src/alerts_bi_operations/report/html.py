"""Self-contained HTML scorecard (design section 6, "Exact MVP output contract").

Rendered only from committed SQL rows. It contains no cross-run trend, delta, leaderboard
or combined v1/v2 volume conclusion: the tool reports one week, and people compare.

Every alert-derived value is HTML-escaped. Messages, node names and justifications are free
text written by other teams and by a model, and none of it is trusted markup.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from typing import Any, Final

from alerts_bi_shared.catalogs import PRINCIPLE_CATALOG
from alerts_bi_shared.phase import PHASE_LABELS
from alerts_bi_shared.timefmt import iso_date, iso_instant
from alerts_bi_shared.window import WINDOW_DAYS, WINDOW_HOURS

__all__ = ["escape_html", "render_scorecard", "rollup_schema"]


def escape_html(value: Any) -> str:
    """Escape a value for HTML text and attribute contexts.

    ``html.escape`` covers ``&``, ``<``, ``>``, ``"`` and ``'``, but writes the apostrophe as
    ``&#x27;``. It is rewritten to the decimal ``&#39;`` so a scorecard is byte-comparable
    against one rendered by the superseded JavaScript implementation. The two entities are
    the same character and render identically; matching the bytes is what lets a diff of two
    scorecards mean "the numbers differ" rather than "the escaper differs".
    """
    if value is None:
        return ""
    return escape(str(value), quote=True).replace("&#x27;", "&#39;")


def _number(value: Any, digits: int = 2) -> str:
    """Format a metric for display, rounding halves away from zero.

    Python's own formatting rounds a tie to the nearest even digit, so 147 alerts over 24
    covered hours - exactly 6.125 - would print as 6.12 where every other tool the team
    uses prints 6.13. Quantizing a Decimal built from the float keeps the tie rule
    conventional without pretending to a precision the value does not have.
    """
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    quantum = Decimal(1).scaleb(-digits)
    return str(Decimal(number).quantize(quantum, rounding=ROUND_HALF_UP))


def _int(value: Any) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}"


def _iso_date(value: Any) -> str:
    if isinstance(value, datetime):
        return iso_date(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")[:10]


def _iso_instant(value: Any) -> str:
    if isinstance(value, datetime):
        return iso_instant(value)
    return str(value or "")


def _num(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def rollup_schema(daily: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Roll daily rows up for one schema using the approved formulas."""
    totals: dict[str, Any] = {
        key: sum(_num(row[key]) for row in daily)
        for key in (
            "alerts",
            "distinct_alerts",
            "flagged_by_rule",
            "flagged_by_rule_distinct",
            "flagged_by_llm",
            "flagged_by_llm_distinct",
            "needs_review",
            "assessed_good",
            "unassessed",
            "phase2_gaps",
            "suppressed",
            "suppression_unmeasured",
            "node_name_numerator",
            "node_name_denominator",
            "key_inflation_numerator",
            "key_inflation_denominator",
        )
    }
    # ``unseen`` is NULL, not zero, where no panel was supplied: it stays None unless at
    # least one bucket carries a measured count.
    for key in ("unseen", "unseen_unmeasured"):
        measured = [row.get(key) for row in daily if row.get(key) is not None]
        totals[key] = sum(_num(v) for v in measured) if measured else None
    node_den = totals["node_name_denominator"]
    key_den = totals["key_inflation_denominator"]
    return {
        **totals,
        # Row counts sum; every distinct measure is published as a per-day rate.
        "alerts_per_hour": totals["alerts"] / WINDOW_HOURS,
        "distinct_per_day": totals["distinct_alerts"] / WINDOW_DAYS,
        "flagged_by_rule_distinct_per_day": totals["flagged_by_rule_distinct"] / WINDOW_DAYS,
        "flagged_by_llm_distinct_per_day": totals["flagged_by_llm_distinct"] / WINDOW_DAYS,
        "needs_review_per_day": totals["needs_review"] / WINDOW_DAYS,
        "assessed_good_per_day": totals["assessed_good"] / WINDOW_DAYS,
        "unassessed_per_day": totals["unassessed"] / WINDOW_DAYS,
        "phase2_gaps_per_day": totals["phase2_gaps"] / WINDOW_DAYS,
        # Diagnostics divide summed numerators by summed denominators, never the mean of
        # already-rounded daily ratios.
        "node_name_ratio": None if node_den == 0 else totals["node_name_numerator"] / node_den,
        "key_inflation_ratio": None
        if key_den == 0
        else totals["key_inflation_numerator"] / key_den,
    }


_STYLES: Final = """
:root { color-scheme: light dark; --fg:#1a1a1a; --muted:#5b6472; --bg:#ffffff;
  --panel:#f6f7f9; --line:#dfe3e8; --accent:#1f5fa9; --warn:#8a5a00; --bad:#a3282d; }
@media (prefers-color-scheme: dark) { :root { --fg:#e8eaed; --muted:#9aa4b2; --bg:#14171c;
  --panel:#1c2027; --line:#2c323b; --accent:#79a9e8; --warn:#e0a94a; --bad:#e57b7b; } }
* { box-sizing: border-box; }
body { margin:0; padding:2rem 1.25rem 4rem; background:var(--bg); color:var(--fg);
  font:15px/1.55 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size:1.6rem; margin:0 0 .25rem; }
h2 { font-size:1.15rem; margin:2.25rem 0 .6rem; padding-bottom:.3rem;
  border-bottom:1px solid var(--line); }
h3 { font-size:1rem; margin:1.25rem 0 .4rem; color:var(--muted); font-weight:600; }
p { margin:.4rem 0; }
.sub { color:var(--muted); margin:0 0 1.25rem; }
.meta { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:.5rem 1.25rem;
  background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:1rem; }
.meta div { display:flex; justify-content:space-between; gap:1rem; font-size:.86rem; }
.meta dt { color:var(--muted); }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:.75rem; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:.85rem 1rem; }
.card .label { color:var(--muted); font-size:.78rem; text-transform:uppercase;
  letter-spacing:.03em; }
.card .value { font-size:1.5rem; font-weight:600; margin-top:.15rem; }
.card .note { color:var(--muted); font-size:.78rem; margin-top:.15rem; }
.table-wrap { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:.86rem; }
th, td { text-align:left; padding:.45rem .6rem; border-bottom:1px solid var(--line);
  white-space:nowrap; }
th { color:var(--muted); font-weight:600; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
td.msg { white-space:normal; min-width:22rem; }
.tag { display:inline-block; padding:.05rem .4rem; border-radius:4px; font-size:.76rem;
  border:1px solid var(--line); background:var(--bg); }
.state-rule_flagged { color:var(--bad); border-color:var(--bad); }
.state-llm_flagged { color:var(--warn); border-color:var(--warn); }
.state-needs_review { color:var(--warn); }
.state-unassessed { color:var(--muted); }
.limits li { margin:.3rem 0; color:var(--muted); font-size:.9rem; }
.empty { color:var(--muted); font-style:italic; }
footer { margin-top:3rem; color:var(--muted); font-size:.8rem; }
"""


def _card(label: str, value: str, note: str | None = None) -> str:
    note_html = f'<div class="note">{escape_html(note)}</div>' if note else ""
    return (
        f'<div class="card"><div class="label">{escape_html(label)}</div>'
        f'<div class="value">{value}</div>{note_html}</div>'
    )


def render_scorecard(
    run: Mapping[str, Any],
    daily: Sequence[Mapping[str, Any]],
    rule_counts: Sequence[Mapping[str, Any]],
    findings: Sequence[Mapping[str, Any]],
    panels: Sequence[Mapping[str, Any]],
    batch_attempts: Sequence[Mapping[str, Any]],
) -> str:
    rollup = {
        schema: rollup_schema([d for d in daily if d["alert_schema"] == schema])
        for schema in ("v1", "v2")
    }

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alerts BI — {escape_html(run["team_display_name"])}</title>
<style>{_STYLES}</style>
</head>
<body>
<main>
<h1>Alerts BI scorecard — {escape_html(run["team_display_name"])}</h1>
<p class="sub">One week of alerting for one team. This report describes the measured window
and nothing else: there is no comparison against a previous run or a baseline.</p>

{_render_run_metadata(run)}
{_render_migration(run, rollup)}
{_render_volume(rollup)}
{_render_diagnostics(rollup)}
{_render_quality(rollup, run)}
{_render_visibility(rollup, panels)}
{_render_rule_breakdown(rule_counts)}
{_render_daily_table(daily)}
{_render_worklist(findings)}
{_render_batch_attempts(batch_attempts)}
{_render_limitations(run)}

<footer>Generated from committed SQL Server rows only. Run {escape_html(run["run_id"])}.</footer>
</main>
</body>
</html>
"""


def _render_run_metadata(run: Mapping[str, Any]) -> str:
    entries = [
        ("Team", run["team_id"]),
        ("Run at (UTC)", _iso_instant(run["run_at"])),
        ("Window start (inclusive)", _iso_instant(run["window_start"])),
        ("Window end (exclusive)", _iso_instant(run["window_end"])),
        ("Registry version", run["registry_version"]),
        ("Registry SHA-256", str(run["registry_sha256"])[:16] + "…"),
        ("Ruleset version", run["ruleset_version"]),
        ("Prompt version", run["prompt_version"]),
        ("Model version", run["model_version"] or "not used"),
        ("LLM assessed", "yes" if run["llm_assessed"] else "no"),
        ("Application version", run["app_version"]),
        ("Run id", str(run["run_id"])[:16] + "…"),
    ]
    rows = "".join(
        f"<div><dt>{escape_html(key)}</dt><dd>{escape_html(value)}</dd></div>"
        for key, value in entries
    )
    return f'<h2>Run metadata</h2>\n<div class="meta">{rows}</div>'


def _render_migration(run: Mapping[str, Any], rollup: Mapping[str, Any]) -> str:
    readiness = (
        "—"
        if run["phase2_readiness_pct"] is None
        else f"{_number(run['phase2_readiness_pct'], 1)}%"
    )
    phase = PHASE_LABELS.get(str(run["phase_derived"]), str(run["phase_derived"]))
    return f"""<h2>Migration phase</h2>
<div class="cards">
  {_card("Derived phase", escape_html(phase), "derived from identity presence, not self-reported")}
  {_card("Phase-2 readiness", readiness, "completion-ready v2 identities / all v2 identities")}
  {_card("v1 distinct alerts/day", _number(rollup["v1"]["distinct_per_day"]))}
  {_card("v2 distinct alerts/day", _number(rollup["v2"]["distinct_per_day"]))}
</div>
<p class="sub">The phase describes only alerts that fired inside this window. It cannot see
silent rules or an external alert inventory.</p>"""


def _render_volume(rollup: Mapping[str, Any]) -> str:
    def block(label: str, r: Mapping[str, Any]) -> str:
        return f"""<h3>{label}</h3>
<div class="cards">
  {_card("Alerts (rows)", _int(r["alerts"]), f"{_number(r['alerts_per_hour'])} per hour over {WINDOW_HOURS}h")}
  {_card("Distinct alerts per day", _number(r["distinct_per_day"]), "sum(daily distinct) / 7")}
</div>"""

    return f"""<h2>Volume</h2>
<p class="sub">Volume is displayed, not scored. No threshold declares an alert rate bad.
Row counts are never compared across schemas.</p>
<p class="sub">A row count reflects how often rows are written. Grafana writes one on every
evaluation, so alerts is evaluation load, not notifications.</p>
{block("v1 (Appchi)", rollup["v1"])}
{block("v2 (Appchi V2)", rollup["v2"])}"""


def _render_diagnostics(rollup: Mapping[str, Any]) -> str:
    def row(schema: str, r: Mapping[str, Any]) -> str:
        node = "—" if r["node_name_ratio"] is None else _number(r["node_name_ratio"], 3)
        key = "—" if r["key_inflation_ratio"] is None else _number(r["key_inflation_ratio"], 3)
        return (
            f"<tr><td>{schema}</td>"
            f'<td class="num">{node}</td>'
            f'<td class="num">{_int(r["node_name_numerator"])}</td>'
            f'<td class="num">{_int(r["node_name_denominator"])}</td>'
            f'<td class="num">{key}</td>'
            f'<td class="num">{_int(r["key_inflation_numerator"])}</td>'
            f'<td class="num">{_int(r["key_inflation_denominator"])}</td></tr>'
        )

    return f"""<h2>Data-quality diagnostics</h2>
<p class="sub">Context only: neither ratio contributes to flagged. A dash means the
denominator was zero, which is not the same as a ratio of zero.</p>
<div class="table-wrap"><table>
<thead><tr><th>Schema</th><th class="num">node_name_ratio</th><th class="num">num</th>
<th class="num">den</th><th class="num">key_inflation_ratio</th><th class="num">num</th>
<th class="num">den</th></tr></thead>
<tbody>{row("v1", rollup["v1"])}{row("v2", rollup["v2"])}</tbody></table></div>"""


def _render_quality(rollup: Mapping[str, Any], run: Mapping[str, Any]) -> str:
    def block(label: str, r: Mapping[str, Any]) -> str:
        return f"""<h3>{label}</h3>
<div class="cards">
  {_card("Flagged by rule (rows)", _int(r["flagged_by_rule"]), f"{_number(r['flagged_by_rule_distinct_per_day'])} distinct/day")}
  {_card("Flagged by LLM (rows)", _int(r["flagged_by_llm"]), f"{_number(r['flagged_by_llm_distinct_per_day'])} distinct/day — advisory")}
  {_card("Needs review /day", _number(r["needs_review_per_day"]))}
  {_card("Assessed good /day", _number(r["assessed_good_per_day"]))}
  {_card("Unassessed /day", _number(r["unassessed_per_day"]), "should be zero")}
  {_card("Phase-2 gaps /day", _number(r["phase2_gaps_per_day"]), "readiness, not quality")}
</div>"""

    unassessed_total = rollup["v1"]["unassessed"] + rollup["v2"]["unassessed"]
    warning = ""
    if unassessed_total > 0:
        detail = (
            "Under exhaustive coverage this is a classifier failure, not a budget decision."
            if run["llm_assessed"]
            else "This run did not call the model, so nothing was examined by it."
        )
        warning = (
            f'<p class="sub"><strong>{_int(unassessed_total)} identity-days are '
            f"unassessed.</strong> {detail}</p>"
        )

    return f"""<h2>Quality</h2>
<p class="sub">Deterministic and LLM findings are kept in separate columns and are never
merged. LLM findings are advisory and do not count toward the headline flagged number.
"Good" is <em>assessed_good</em> — it is never inferred by subtracting flagged from total,
because that would count what nobody looked at as fine.</p>
{warning}
{block("v1 (Appchi)", rollup["v1"])}
{block("v2 (Appchi V2)", rollup["v2"])}"""


def _render_visibility(rollup: Mapping[str, Any], panels: Sequence[Mapping[str, Any]]) -> str:
    suppressed = rollup["v1"]["suppressed"] + rollup["v2"]["suppressed"]
    unmeasured = rollup["v1"]["suppression_unmeasured"] + rollup["v2"]["suppression_unmeasured"]

    def per_schema(key: str) -> str:
        parts = []
        for schema in ("v1", "v2"):
            value = rollup[schema][key]
            shown = _int(value) if value is not None else "&mdash; no panel supplied"
            parts.append(f'<span class="unseen-{schema}">{schema}: {shown}</span>')
        return " &middot; ".join(parts)

    unseen_card = per_schema("unseen")
    unseen_unmeasured_card = per_schema("unseen_unmeasured")
    if panels:
        panel_rows = "".join(
            f"<tr><td>{escape_html(p['panel_id'])}</td><td>{escape_html(p['alert_schema'])}</td>"
            f'<td class="num">{_int(p["suppression_leaves"])}</td>'
            f'<td class="num">{_int(p["unmeasured_leaves"])}</td>'
            f"<td>{escape_html(str(p['sql_text_hash'])[:12])}…</td></tr>"
            for p in panels
        )
    else:
        panel_rows = (
            '<tr><td colspan="5" class="empty">No panel queries were supplied for this '
            "team.</td></tr>"
        )

    return f"""<h2>Dashboard visibility</h2>
<p class="sub">Suppressed alerts are the team's own exclusion clauses quoted back to it —
rule 5, and the phase-0 work list. <em>suppressed</em> is a subset of flagged_by_rule, not
an addition to it. <em>suppression_unmeasured</em> counts leaves that were detected but
could not be evaluated safely, so an under-reported number is visible as under-reported
rather than passing for zero.</p>
<div class="cards">
  {_card("Suppressed (rows)", _int(suppressed))}
  {_card("Suppression unmeasured (leaves)", _int(unmeasured))}
  {_card("Unseen (rows no panel shows)", unseen_card, "owned rows hidden by every supplied panel")}
  {_card("Unseen unmeasured (leaves)", unseen_unmeasured_card)}
</div>
<h3>Supplied panels</h3>
<div class="table-wrap"><table>
<thead><tr><th>Panel</th><th>Schema</th><th class="num">Suppression leaves</th>
<th class="num">Unmeasured</th><th>SQL hash</th></tr></thead>
<tbody>{panel_rows}</tbody></table></div>"""


def _render_rule_breakdown(rule_counts: Sequence[Mapping[str, Any]]) -> str:
    totals: dict[tuple[str, str], dict[str, int]] = {}
    for row in rule_counts:
        key = (str(row["alert_schema"]), str(row["rule_id"]))
        entry = totals.setdefault(key, {"count": 0, "distinct": 0})
        entry["count"] += int(row["match_count"])
        entry["distinct"] += int(row["distinct_count"])

    ordered = sorted(totals.items(), key=lambda item: (item[0][0], int(item[0][1][1:])))
    if ordered:
        body = "".join(
            f"<tr><td>{escape_html(schema)}</td><td>{escape_html(rule_id)}</td>"
            f'<td class="num">{_int(entry["count"])}</td>'
            f'<td class="num">{_number(entry["distinct"] / WINDOW_DAYS)}</td></tr>'
            for (schema, rule_id), entry in ordered
        )
    else:
        body = (
            '<tr><td colspan="4" class="empty">No deterministic findings in this window.</td></tr>'
        )

    principles = "".join(
        f"<tr><td>{escape_html(p.id)}</td><td>{escape_html(p.set)}</td>"
        f'<td class="msg">{escape_html(p.text)}</td></tr>'
        for p in PRINCIPLE_CATALOG
    )

    return f"""<h2>Rule and principle breakdown</h2>
<div class="table-wrap"><table>
<thead><tr><th>Schema</th><th>Rule</th><th class="num">Matching rows</th>
<th class="num">Distinct identities/day</th></tr></thead>
<tbody>{body}</tbody></table></div>
<h3>LLM principle catalogue</h3>
<div class="table-wrap"><table>
<thead><tr><th>ID</th><th>Set</th><th>Principle</th></tr></thead>
<tbody>{principles}</tbody></table></div>"""


def _render_daily_table(daily: Sequence[Mapping[str, Any]]) -> str:
    if daily:
        body = "".join(
            f"<tr><td>{escape_html(d['alert_schema'])}</td>"
            f"<td>{escape_html(_iso_date(d['snapshot_date']))}</td>"
            f'<td class="num">{_number(d["covered_hours"], 1)}</td>'
            f'<td class="num">{_int(d["alerts"])}</td>'
            f'<td class="num">{_int(d["distinct_alerts"])}</td>'
            f'<td class="num">{_number(d["alerts_per_hour"])}</td>'
            f'<td class="num">{_int(d["flagged_by_rule"])}</td>'
            f'<td class="num">{_int(d["flagged_by_llm"])}</td>'
            f'<td class="num">{_int(d["suppressed"])}</td></tr>'
            for d in daily
        )
    else:
        body = '<tr><td colspan="9" class="empty">No daily rows.</td></tr>'

    return f"""<h2>Daily breakdown</h2>
<p class="sub">A rolling 168-hour window normally touches eight UTC dates, so the first and
last buckets are partial. Each row shows the hours it actually covers.</p>
<div class="table-wrap"><table>
<thead><tr><th>Schema</th><th>Date (UTC)</th><th class="num">Hours</th><th class="num">Alerts</th>
<th class="num">Distinct</th><th class="num">Alerts/h</th><th class="num">Rule</th>
<th class="num">LLM</th><th class="num">Suppressed</th></tr></thead>
<tbody>{body}</tbody></table></div>"""


def _render_worklist(findings: Sequence[Mapping[str, Any]]) -> str:
    actionable = [
        f for f in findings if f["quality_state"] in ("rule_flagged", "llm_flagged", "needs_review")
    ]
    if actionable:
        rows = []
        for f in actionable:
            rules = " ".join(x for x in (f["core_rule_ids"], f["readiness_rule_ids"]) if x)
            principle = f["llm_principle_id"] or ""
            cite = "" if principle in ("", "NONE") else principle
            confidence = f" ({escape_html(f['llm_confidence'])})" if f["llm_confidence"] else ""
            rows.append(
                "<tr>"
                f"<td>{escape_html(f['alert_schema'])}</td>"
                f"<td>{escape_html(f['application'])}</td>"
                f"<td>{escape_html(f['key_field'])}</td>"
                f'<td><span class="tag state-{escape_html(f["quality_state"])}">'
                f"{escape_html(f['quality_state'])}</span></td>"
                f"<td>{escape_html(rules)}</td>"
                f"<td>{escape_html(cite)}{confidence}</td>"
                f'<td class="num">{_int(f["row_count"])}</td>'
                f'<td class="msg">{escape_html(f["message"])}</td>'
                "</tr>"
            )
        body = "".join(rows)
    else:
        body = (
            '<tr><td colspan="8" class="empty">Nothing flagged or awaiting review in this '
            "window.</td></tr>"
        )

    return f"""<h2>Work list</h2>
<p class="sub">One row per distinct alert identity — never divided by seven. The full,
untruncated values are in <code>alert_worklist.csv</code>.</p>
<div class="table-wrap"><table>
<thead><tr><th>Schema</th><th>Application</th><th>key_field</th><th>State</th><th>Rules</th>
<th>Principle</th><th class="num">Rows</th><th>Message</th></tr></thead>
<tbody>{body}</tbody></table></div>"""


def _render_batch_attempts(attempts: Sequence[Mapping[str, Any]]) -> str:
    if not attempts:
        return ""
    failed = [a for a in attempts if a["status"] != "succeeded"]
    batches = len({a["batch_id"] for a in attempts})
    return f"""<h2>LLM batch attempts</h2>
<p class="sub">{_int(len(attempts))} attempt(s) across {_int(batches)} batch(es);
{_int(len(failed))} failed. A batch receives three total attempts and is retried as a
whole; after the third failure every alert in it becomes unassessed.</p>"""


def _render_limitations(run: Mapping[str, Any]) -> str:
    not_assessed = (
        ""
        if run["llm_assessed"]
        else (
            "<li>This run did not call the model, so every eligible identity is "
            "<code>unassessed</code>. An empty LLM column here means <em>not examined</em>, "
            "not <em>nothing found</em>.</li>"
        )
    )
    return f"""<h2>Limitations</h2>
<ul class="limits">
  <li>This is a single week. There is no trend, delta, baseline or improvement percentage,
      and no cross-team leaderboard.</li>
  <li>v1 and v2 row counts are never added together. A row count reflects how often
      rows are written, so raw volume is not comparable across schemas.</li>
  <li>v1 and v2 <code>distinct_alerts</code> are not like-for-like: the v1 key is
      application + object + node_name, while the v2 key hashes roughly a dozen fields.</li>
  <li>Every distinct figure is a per-day rate. A seven-day total would be seven times a
      one-day total for arithmetic reasons alone.</li>
  <li>LLM findings are advisory and excluded from the headline flagged number until a
      human review measures precision at or above 95% for a named prompt and model
      version.</li>
  <li>Enriching a v2 alert mints a new <code>key_field</code>, so a team that just added
      <code>impact</code> or <code>runbook_url</code> can look briefly worse. The artefact
      clears within a week.</li>
  <li>R6 flags one alert's firing episodes (stuck, spamming or flapping); it never scores a
      team's total volume.</li>
  <li>Phase and readiness describe only alerts that fired in this window; silent rules and
      the external alert inventory are invisible to this tool.</li>
  {not_assessed}
</ul>"""
