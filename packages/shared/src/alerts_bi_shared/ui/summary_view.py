"""The team summary's shared widgets (team summary spec section 4), rendered on the server.

One renderer serves both reading surfaces. The reader portal shows a published week under
the portal's rules: weekly totals, no per-day rate, no internals. The operator admin app
shows any completed run and adds what only operators need - per-day rates and unmeasured
clause counts - when ``summary.inputs.surface == "admin"``.

Everything here is markup from a :class:`~alerts_bi_shared.insights.TeamSummary`; nothing reads a
database. Every alert value goes through :func:`~alerts_bi_shared.ui.html.h`, and an alert-rule URL
becomes a link only through :func:`~alerts_bi_shared.ui.html.safe_link`. There is no script and no
inline ``style``: bars are SVG geometry coloured by CSS classes. v1 and v2 are never added
together.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import date, timedelta
from hashlib import sha256
from typing import Final

from alerts_bi_shared.catalogs import (
    R6_API_MIN_SPAN,
    R6_API_RATE_WINDOW,
    R6_API_SPAM_PER_24H,
    R6_FLAP_CYCLES,
    R6_FLAP_WINDOW,
    R6_STUCK_OPEN,
    V2_READINESS_RULE_IDS,
)
from alerts_bi_shared.insights import AlertRow, Estimate, FireRow, SchemaTotals, TeamSummary
from alerts_bi_shared.insights.aggregate import primary_rule_counts
from alerts_bi_shared.insights.stored import fact, patterns
from alerts_bi_shared.ui.charts import TIMES, Segment, level_bar, stacked_bar
from alerts_bi_shared.ui.explain import (
    QUALITY_STATE_LABELS,
    dominant_r6_pattern,
    format_instant,
    principle_next_step,
    principle_title,
    r6_next_step,
    rule_explanation,
)
from alerts_bi_shared.ui.html import PHASE_STEPS, SCHEMA_NAMES, h, safe_link
from alerts_bi_shared.ui.slides import render_slides

__all__ = ["NOT_MEASURED", "format_projected_week", "render_summary_sections"]

RuleLink = Callable[[str | None], str]

SCHEMAS = ("v1", "v2")
#: How many rows the longer widgets show; the work list below has every alert.
TOP_APPLICATIONS = 10
TOP_FIRING_PER_SCHEMA = 4
TOP_LISTED = 8
TOP_RULE_APPLICATIONS = 3

#: Each phase's completion criterion (design section 3.4).
DONE_WHEN = {
    "phase_0": "nothing is hidden by your own panels, volume is low and every alert is mapped.",
    "phase_1": "no v1 alert fires.",
    "phase_2": "every critical v2 alert has an impact and a runbook.",
    "done": "all of the above hold.",
}

_STATES = (
    ("rule_flagged", "q-rule"),
    ("llm_flagged", "q-model"),
    ("needs_review", "q-review"),
    ("assessed_good", "q-good"),
    ("unassessed", "q-un"),
)


# ------------------------------------------------------------------ small helpers


def format_projected_week(day: date) -> str:
    """``week of 12 Oct 2026`` - the estimate is only ever as precise as a week."""
    return f"week of {day.day} {day:%b %Y}"


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def _dec(value: float) -> str:
    """At most one decimal place, without a trailing ``.0``: ``0``, ``0.5``, ``1,250``."""
    text = f"{value:,.1f}"
    return text[:-2] if text.endswith(".0") else text


def _hours(span: timedelta) -> str:
    return _dec(span.total_seconds() / 3600)


#: The three R6 patterns in the legend's display order (the rule itself applies flapping,
#: then spamming, then stuck; design section 7.14). The
#: copy is built from the catalogue constants R6 itself uses, so the two cannot drift.
FIRE_THRESHOLDS = (
    (
        "stuck",
        "Stuck",
        f"kept firing with no clear for \u2265{_hours(R6_STUCK_OPEN)} h, from its first to its "
        "last firing row",
    ),
    (
        "spamming",
        "Spamming",
        f"an API alert at \u2265{_dec(R6_API_SPAM_PER_24H)} events per "
        f"{_hours(R6_API_RATE_WINDOW)} h over \u2265{_hours(R6_API_MIN_SPAN)} h",
    ),
    (
        "flapping",
        "Flapping",
        f"\u2265{R6_FLAP_CYCLES} fire\u2192clear cycles in {_hours(R6_FLAP_WINDOW)} h",
    ),
)


def _rule_order(rule_id: str) -> tuple[int, str]:
    digits = rule_id[1:]
    return (int(digits) if digits.isdigit() else 10**6, rule_id)


def _is_readiness(rule_id: str) -> bool:
    return rule_id in V2_READINESS_RULE_IDS


def _chip(schema: str) -> str:
    return f'<span class="chip {h(schema)}">{h(schema)}</span>'


def _rule_title(rule_id: str) -> str:
    title = rule_explanation(rule_id, None).title
    return "" if title == rule_id else title


def _rule_label(rule_id: str) -> str:
    """``R1 Generic message``, or the bare id for a rule with no copy yet."""
    title = _rule_title(rule_id)
    return f"{rule_id} {title}" if title else rule_id


def _next_step(rule_id: str) -> str:
    return rule_explanation(rule_id, None).next_step


def _rule_step(
    rule_id: str,
    alerts: Iterable[AlertRow],
    summary: TeamSummary | None = None,
    schema: str | None = None,
) -> str:
    """The next step for a rule across these alerts. R6's depends on the firing pattern, so it
    is the step of the pattern most of the rule's alerts carry (send-once only for spamming)."""
    if rule_id != "R6":
        return _next_step(rule_id)
    if summary is not None and summary.inputs.facts is not None:
        return r6_next_step(
            dominant_r6_pattern(
                (p, pair[1]) for p, pair in patterns(summary.inputs, schema).items()
            )
        )
    return r6_next_step(
        dominant_r6_pattern(
            (a.fire_pattern, a.row_count) for a in alerts if "R6" in a.core_rule_ids
        )
    )


def _card(css: str, title: str, intro: str, body: str) -> str:
    return (
        f'<article class="card sm {css}"><div class="sm-h"><h3>{title}</h3>'
        f"{f'<p>{intro}</p>' if intro else ''}</div>{body}</article>"
    )


def _empty(text: str) -> str:
    return f'<p class="sub">{h(text)}</p>'


def _alert_line(alert: AlertRow) -> str:
    """Message, then where it comes from. Alert text is escaped."""
    return (
        f'<span class="msg">“{h(alert.message or "(no message)")}”</span>'
        f'<span class="ctx"><span class="src">{h(alert.application)} &rsaquo; '
        f"{h(alert.component or '—')}</span>{_chip(alert.schema)}</span>"
    )


def _span(hours: float) -> str:
    if hours < 1:
        return f"{round(hours * 60)} min"
    if hours < 48:
        return f"{_dec(hours)} h"
    return f"{_dec(hours / 24)} days"


# ------------------------------------------------------------------ 1-3: volume and quality


def _glance(summary: TeamSummary, schema: str) -> str:
    totals = summary.inputs.schemas[schema]
    admin = summary.inputs.surface == "admin"
    heading = f'<h3><span class="chip {schema}">{schema}</span>{SCHEMA_NAMES[schema]}</h3>'
    if totals.events == 0 and totals.distinct_alerts == 0:
        return (
            f'<article class="card schema {schema}">{heading}'
            f'<p class="sub">No {schema} alerts fired this week.</p></article>'
        )

    if admin:
        per_day = "—" if totals.distinct_per_day is None else _dec(totals.distinct_per_day)
        kpis = (
            '<div class="kpis k3">'
            f'<div class="kpi"><span class="n">{per_day}</span>'
            '<span class="u">distinct alerts per day</span>'
            '<span class="d">sum of daily distinct alerts ÷ 7</span></div>'
            f'<div class="kpi"><span class="n">{totals.events:,}</span>'
            '<span class="u">rows</span><span class="d">every firing, repeats included</span></div>'
            f'<div class="kpi"><span class="n">{_dec(totals.events / 168)}</span>'
            '<span class="u">rows an hour</span><span class="d">rows ÷ 168 hours</span></div>'
            "</div>"
        )
    else:
        kpis = (
            '<div class="kpis">'
            f'<div class="kpi"><span class="n">{totals.distinct_alerts:,}</span>'
            '<span class="u">distinct alerts this week</span>'
            '<span class="d">each alert counted once, however often it fired</span></div>'
            f'<div class="kpi"><span class="n">{totals.events:,}</span>'
            '<span class="u">alert events this week</span>'
            '<span class="d">every firing, repeats included</span></div>'
            "</div>"
        )

    share = (
        f" ({round(100 * totals.rule_flagged_events / totals.events)}%)"
        if totals.events and totals.rule_flagged_events is not None
        else ""
    )
    flagged_events = (
        "Application breakdown unavailable"
        if totals.rule_flagged_events is None
        else f"{totals.rule_flagged_events:,} of {totals.events:,}"
    )
    facts = [
        f"<dt>{'Rows' if admin else 'Events'} with a rule finding</dt>"
        f'<dd class="num">{flagged_events}'
        f"{share if admin else ''}</dd>",
        "<dt>Alerts with a rule finding</dt>"
        f'<dd class="num">{totals.rule_flagged_alerts:,} of {totals.distinct_alerts:,}</dd>',
    ]
    if totals.suppressed:
        facts.append(
            f'<dt>Hidden by your own dashboard</dt><dd class="num">{totals.suppressed:,} '
            f"{'rows' if admin else 'events'}</dd>"
        )
    if schema == "v2":
        facts.append(
            "<dt>v2 readiness gaps</dt>"
            f"<dd>{_plural(totals.readiness_gaps, 'alert')} missing an impact or a usable "
            "runbook. Counted apart from quality.</dd>"
        )
    examined = stacked_bar(
        [
            Segment(css, QUALITY_STATE_LABELS[state], int(totals.states.get(state, 0)))
            for state, css in _STATES
        ],
        label=f"{schema} alerts by review outcome",
    )
    return (
        f'<article class="card schema {schema}">{heading}{kpis}'
        f'<dl class="facts">{"".join(facts[:2])}</dl>'
        f'<div><div class="eyebrow">What we found in these '
        f"{_plural(totals.distinct_alerts, 'alert')}</div>{examined}</div>"
        f'<dl class="facts">{"".join(facts[2:])}</dl>'
        "</article>"
    )


# ------------------------------------------------------------------ 4: why flagged


def _bars(
    rows: Sequence[tuple[str, str, str, int, int, float | None]], *, css: str, admin: bool
) -> str:
    """``(label_html, schema, ..., alerts, events, per_day)`` rows as one bar list."""
    if not rows:
        return _empty("None this week.")
    maximum = max(alerts for *_, alerts, _events, _per_day in rows)
    items = []
    for label, schema, _key, alerts, events, per_day in rows:
        extra = f" · {_dec(per_day)} per day" if admin and per_day is not None else ""
        items.append(
            f'<li><span class="bl">{label} {_chip(schema)}</span>'
            f"{level_bar(alerts, maximum, css=css)}"
            f'<span class="bv"><b>{_plural(alerts, "alert")}</b> · {events:,} events{extra}</span>'
            "</li>"
        )
    return f'<ul class="bars">{"".join(items)}</ul>'


#: Donut geometry: a 120-unit square, outer and inner radius of the ring.
_DONUT_C, _DONUT_R, _DONUT_HOLE = 60.0, 56.0, 38.0


def _at(radius: float, angle: float) -> str:
    return f"{_DONUT_C + radius * math.cos(angle):.3f} {_DONUT_C + radius * math.sin(angle):.3f}"


def _ring_path() -> str:
    """A full ring as two closed circles; ``evenodd`` leaves the hole empty."""
    top, bottom = -math.pi / 2, math.pi / 2
    r, hole = _DONUT_R, _DONUT_HOLE
    return (
        f"M {_at(r, top)} A {r} {r} 0 1 1 {_at(r, bottom)} A {r} {r} 0 1 1 {_at(r, top)} Z "
        f"M {_at(hole, top)} A {hole} {hole} 0 1 0 {_at(hole, bottom)} "
        f"A {hole} {hole} 0 1 0 {_at(hole, top)} Z"
    )


def _arc_path(start: float, end: float) -> str:
    """One ring segment from ``start`` to ``end`` radians, clockwise from the top."""
    large = 1 if end - start > math.pi else 0
    r, hole = _DONUT_R, _DONUT_HOLE
    return (
        f"M {_at(r, start)} A {r} {r} 0 {large} 1 {_at(r, end)} "
        f"L {_at(hole, end)} A {hole} {hole} 0 {large} 0 {_at(hole, start)} Z"
    )


def _share(n: int, total: int) -> str:
    """A whole percent, rounded half up exactly (never banker's rounding); a nonzero slice
    too small to reach 1% reads ``<1%`` rather than 0, and only the whole reads 100%: a
    partial slice is clamped to 99%. Shares need not sum to 100."""
    percent = (200 * n + total) // (2 * total)
    if n and percent == 0:
        return "<1%"
    if n < total:
        percent = min(percent, 99)
    return f"{percent}%"


def _donut(summary: TeamSummary, schema: str, rule_link: RuleLink) -> str:
    """One schema's rule-flagged alerts by primary rule. Never combined with the other."""
    counts = (
        sorted(
            fact(summary.inputs, schema, "primary").items(), key=lambda item: _rule_order(item[0])
        )
        if summary.inputs.facts
        else primary_rule_counts(summary.inputs.alerts, schema)
    )
    total = sum(n for _, n in counts)
    if not total:
        return (
            f'<div class="donut-one">{_empty(f"No rule-flagged {schema} alerts this week.")}</div>'
        )
    slices, angle = [], -math.pi / 2
    for rule_id, n in counts:
        css = f"slice {rule_id.lower()}"
        label = f"<title>{h(rule_id)}: {_plural(n, 'alert')}</title>"
        if n == total:
            slices.append(
                f'<path class="{css}" fill-rule="evenodd" d="{_ring_path()}">{label}</path>'
            )
            break
        end = angle + 2 * math.pi * n / total
        slices.append(f'<path class="{css}" d="{_arc_path(angle, end)}">{label}</path>')
        angle = end
    name = SCHEMA_NAMES[schema]
    noun = "rule-flagged alert" if total == 1 else "rule-flagged alerts"
    svg = (
        f'<svg class="donut" viewBox="0 0 120 120" role="img" '
        f'aria-label="{h(name)}: {total:,} {noun}">{"".join(slices)}'
        f'<text class="dl-schema" x="60" y="45" text-anchor="middle">{h(name)}</text>'
        f'<text class="dl-n" x="60" y="66" text-anchor="middle">{total:,}</text>'
        # Two short lines, so the caption stays inside the hole.
        f'<text class="dl-u" x="60" y="77" text-anchor="middle">rule-flagged</text>'
        f'<text class="dl-u" x="60" y="86" text-anchor="middle">'
        f"{'alert' if total == 1 else 'alerts'}</text></svg>"
    )
    legend = "".join(
        f'<li class="dk"><svg viewBox="0 0 9 9" aria-hidden="true"><rect class="{rule_id.lower()}" '
        f'width="9" height="9" rx="2"/></svg><a href="{rule_link(rule_id)}">{h(rule_id)}</a> '
        f'{h(_rule_title(rule_id))} <b class="num">{n:,}</b> '
        f'<span class="sub">{_share(n, total)}</span></li>'
        for rule_id, n in counts
    )
    return (
        f'<div class="donut-one"><div class="eyebrow">{_chip(schema)} {h(name)}</div>'
        f'<div class="donut-body">{svg}<ul class="dlegend">{legend}</ul></div></div>'
    )


def _view_toggle(summary: TeamSummary) -> tuple[str, str]:
    """Radio ids for the Bars / Donut toggle: stable for one team's week, distinct per week,
    so two summaries on one page never share a group."""
    inputs = summary.inputs
    key = f"{inputs.team_id}|{inputs.window_end.isoformat()}|{inputs.surface}"
    group = "why-" + sha256(key.encode()).hexdigest()[:10]
    return group, (
        f'<input type="radio" class="vt-bars" name="{group}" id="{group}-bars" checked>'
        f'<label for="{group}-bars">Bars</label>'
        f'<input type="radio" class="vt-donut" name="{group}" id="{group}-donut">'
        f'<label for="{group}-donut">Donut</label>'
    )


def _why(summary: TeamSummary, rule_link: RuleLink) -> str:
    admin = summary.inputs.surface == "admin"
    rules = sorted(summary.inputs.rules, key=lambda r: (_rule_order(r.rule_id), r.schema))

    def rule_rows(readiness: bool) -> list[tuple[str, str, str, int, int, float | None]]:
        return [
            (
                f'<a href="{rule_link(r.rule_id)}">{h(r.rule_id)}</a> {h(_rule_title(r.rule_id))}',
                r.schema,
                r.rule_id,
                r.alerts,
                r.events,
                r.distinct_per_day,
            )
            for r in rules
            if _is_readiness(r.rule_id) is readiness
        ]

    model: dict[tuple[str, str], list[AlertRow]] = defaultdict(list)
    for alert in summary.inputs.alerts:
        if alert.quality_state == "llm_flagged" and alert.llm_principle_id:
            model[(alert.llm_principle_id, alert.schema)].append(alert)
    model_rows: list[tuple[str, str, str, int, int, float | None]] = [
        (
            h(principle_title(principle)),
            schema,
            principle,
            len(members),
            sum(a.row_count for a in members),
            None,
        )
        for (principle, schema), members in sorted(
            model.items(), key=lambda item: (_rule_order(item[0][0]), item[0][1])
        )
    ]
    if summary.inputs.facts is not None:
        model_rows = sorted(
            [
                (h(principle_title(p)), s, p, pair[0], pair[1], None)
                for s in SCHEMAS
                for p, pair in fact(summary.inputs, s, "model").items()
            ],
            key=lambda row: (_rule_order(row[2]), row[1]),
        )
    readiness_and_model = (
        f'<div><div class="eyebrow">Phase-2 readiness</div>'
        f"{_bars(rule_rows(True), css='f-ready', admin=admin)}</div>"
        f'<div><div class="eyebrow">Model findings (advisory)</div>'
        f"{_bars(model_rows, css='f-model', admin=admin)}</div>"
    )
    bars_view = (
        '<div class="groups">'
        f'<div><div class="eyebrow">Quality</div>{_bars(rule_rows(False), css="f-rule", admin=admin)}</div>'
        f"{readiness_and_model}"
        "</div>"
    )
    donut_view = (
        f'<div class="donuts">{"".join(_donut(summary, s, rule_link) for s in SCHEMAS)}</div>'
        '<p class="sub">Each alert is counted once, under its first rule in catalogue order. '
        "The bars view shows every rule an alert matches. Model findings and v2 readiness "
        "gaps are not part of the donut.</p>"
        f'<div class="groups">{readiness_and_model}</div>'
    )
    _, toggle = _view_toggle(summary)
    body = (
        f'<div class="vtoggle">{toggle}'
        f'<div class="view-bars">{bars_view}</div>'
        f'<div class="view-donut">{donut_view}</div></div>'
    )
    return _card(
        "why",
        "Why alerts were flagged",
        "Alerts this week per rule and schema. Model findings are advisory and counted apart.",
        body,
    )


# ------------------------------------------------------------------ 5: key findings


def _key_findings(summary: TeamSummary, rule_link: RuleLink) -> str:
    if not summary.key_findings:
        return _card("kf", "Key findings", "", _empty("Nothing stands out this week."))
    items = []
    for finding in summary.key_findings:
        step = finding.fix
        if step is None and finding.rule_filter:
            step = _rule_step(finding.rule_filter, summary.inputs.alerts, summary) or None
        action = f'<div class="next"><b>Next step:</b> {h(step)}</div>' if step else ""
        show = (
            f'<a class="more" href="{rule_link(finding.rule_filter)}">Show these alerts</a>'
            if finding.rule_filter
            else ""
        )
        items.append(
            f'<li class="{h(finding.kind)}"><b>{h(finding.title)}</b>'
            f"<p>{h(finding.body)}</p>{action}{show}</li>"
        )
    return _card("kf", "Key findings", "", f'<ol class="kf">{"".join(items)}</ol>')


# ------------------------------------------------------------------ 6: by application


def _table(head: Iterable[str], rows: Iterable[str], css: str = "") -> str:
    header = "".join(f'<th scope="col">{cell}</th>' for cell in head)
    return (
        f'<div class="table-wrap"><table class="grid {css}"><thead><tr>{header}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _by_application(summary: TeamSummary, rule_link: RuleLink) -> str:
    apps = summary.by_application
    if not apps:
        return _card("apps", "Noisy alerts by application", "", _empty("No alerts this week."))

    def found(alerts: int, events: int) -> str:
        if not alerts:
            return '<span class="sub">—</span>'
        return f"{_plural(alerts, 'alert')} · {_plural(events, 'event')}"

    rows = []
    for app in apps[:TOP_APPLICATIONS]:
        rules = (
            " ".join(
                f'<a class="chip rule" href="{rule_link(rid)}">{h(rid)}</a>' for rid in app.rules
            )
            or '<span class="sub">—</span>'
        )
        rows.append(
            f"<tr><td>{h(app.application)}</td><td>{_chip(app.schema)}</td>"
            f'<td class="num">{app.alerts:,}</td><td class="num">{app.events:,}</td>'
            f'<td class="num">{found(app.rule_flagged_alerts, app.rule_flagged_events)}</td>'
            f'<td class="num">{found(app.llm_flagged_alerts, app.llm_flagged_events)}</td>'
            f"<td>{rules}</td></tr>"
        )
    more = ""
    if len(apps) > TOP_APPLICATIONS:
        per_schema = " and ".join(
            f"{sum(1 for app in apps if app.schema == schema):,} {schema}"
            for schema in SCHEMAS
            if any(app.schema == schema for app in apps)
        )
        more = (
            f'<p class="sub">The {TOP_APPLICATIONS} with the most flagged events, of '
            f"{per_schema} applications.</p>"
        )
    return _card(
        "apps",
        "Noisy alerts by application",
        "Rule findings and advisory model findings are counted in separate columns.",
        _table(
            (
                "Application",
                "Schema",
                "Alerts",
                "Events",
                "Rule-flagged",
                "Model (advisory)",
                "Rules seen",
            ),
            rows,
        )
        + more,
    )


# ------------------------------------------------------------------ 7: how often alerts fire


def _open_for(row: FireRow) -> str:
    return '<span class="sub">\u2014</span>' if row.open_hours is None else _span(row.open_hours)


def _fire(summary: TeamSummary) -> str:
    legend = "".join(
        f'<li><span class="pill {key}">{label}</span>: {h(text)}</li>'
        for key, label, text in FIRE_THRESHOLDS
    )
    legend_html = (
        f'<ul class="thresholds">{legend}</ul>'
        '<p class="sub">Grafana records an event on every evaluation, so a Grafana alert\'s '
        "event count shows how often it is evaluated, not how often it notifies. Active time "
        "runs from the first to the last event; open for is the span of the firing events "
        "since the last clear.</p>"
    )
    if not summary.fire:
        return _card(
            "fire", "How often alerts fire", "", _empty("No alerts this week.") + legend_html
        )

    def block(schema: str) -> str:
        top = [row for row in summary.fire if row.alert.schema == schema][:TOP_FIRING_PER_SCHEMA]
        heading = f'<div class="eyebrow">{_chip(schema)} {SCHEMA_NAMES[schema]}</div>'
        if not top:
            return heading + _empty(f"No {schema} alerts this week.")
        rows = []
        for row in top:
            pattern = (
                f'<span class="pill {h(row.pattern)}">{h(row.pattern)}</span>'
                if row.pattern
                else '<span class="sub">\u2014</span>'
            )
            if row.pattern == "spamming" and row.events_per_24h is not None:
                pattern += (
                    f' <span class="sub">{h(_dec(row.events_per_24h))} events per '
                    f"{h(_hours(R6_API_RATE_WINDOW))} h over {h(_dec(row.span_hours))} h</span>"
                )
            rows.append(
                f'<tr><td class="alert">{_alert_line(row.alert)}</td>'
                f'<td class="num">{row.alert.row_count:,}</td>'
                f'<td class="num">{_span(row.span_hours)}</td>'
                f'<td class="num">{row.alert.max_clear_cycles_24h:,}</td>'
                f'<td class="num">{_open_for(row)}</td>'
                f"<td>{pattern}</td></tr>"
            )
        return heading + _table(
            (
                "Alert",
                "Events",
                "Active",
                "Clear cycles (most in 24 h)",
                "Open for",
                "Pattern",
            ),
            rows,
            "fire",
        )

    return _card(
        "fire",
        "How often alerts fire",
        f"Each schema's {TOP_FIRING_PER_SCHEMA} alerts with the most events this week.",
        "".join(block(schema) for schema in SCHEMAS) + legend_html,
    )


# ------------------------------------------------------------------ 8: biggest single source


def _fixes(alert: AlertRow, rule_link: RuleLink) -> list[str]:
    steps = []
    for rule_id in alert.core_rule_ids:
        step = r6_next_step(alert.fire_pattern) if rule_id == "R6" else _next_step(rule_id)
        steps.append(
            f"<li><b>{h(_rule_label(rule_id))}.</b> {h(step)} "
            f'<a href="{rule_link(rule_id)}">Show alerts with {h(rule_id)}</a></li>'
        )
    if alert.quality_state == "llm_flagged" and alert.llm_principle_id:
        steps.append(
            f"<li><b>{h(principle_title(alert.llm_principle_id))} (advisory).</b> "
            f"{h(principle_next_step(alert.llm_principle_id))}</li>"
        )
    for rule_id in alert.readiness_rule_ids:
        steps.append(f"<li><b>{h(_rule_label(rule_id))}.</b> {h(_next_step(rule_id))}</li>")
    return steps


def _biggest(summary: TeamSummary, rule_link: RuleLink) -> str:
    alert = summary.biggest
    if alert is None:
        return _card("big", "Biggest single source", "", _empty("No alerts this week."))
    target = safe_link(alert.alert_rule_url)
    if target is not None:
        rule = f'<a href="{h(target)}" rel="noopener noreferrer nofollow" target="_blank">{h(target)}</a>'
    elif alert.alert_rule_url:
        rule = f'<span class="missing">{h(alert.alert_rule_url)} · not a usable http(s) link</span>'
    else:
        rule = '<span class="na">none</span>'
    fields = [
        ("Application", h(alert.application)),
        ("Component", h(alert.component or "—")),
        ("Schema", f"{_chip(alert.schema)} {SCHEMA_NAMES.get(alert.schema, '')}"),
        ("Severity", h(alert.severity or "—")),
        ("Provider", h(alert.provider or "—")),
        ("Node", h(alert.node_name or "not set")),
        ("Alert rule", rule),
        ("Events this week", f"{alert.row_count:,}"),
        ("First seen", h(format_instant(alert.first_seen))),
        ("Last seen", h(format_instant(alert.last_seen))),
    ]
    doc = "".join(f"<dt>{label}</dt><dd>{value}</dd>" for label, value in fields)
    steps = _fixes(alert, rule_link)
    fix = (
        f'<div class="eyebrow">What to change</div><ul class="fixes">{"".join(steps)}</ul>'
        if steps
        else _empty("No finding on this alert this week.")
    )
    return _card(
        "big",
        "Biggest single source",
        "The one alert with the most events this week.",
        f'<p class="msg">“{h(alert.message or "(no message)")}”</p><dl class="doc">{doc}</dl>{fix}',
    )


# ------------------------------------------------------------------ 9: flagged by rule


def _top_applications(
    alerts: Sequence[AlertRow], schema: str, rule_id: str, summary: TeamSummary | None = None
) -> str:
    events: dict[str, int] = defaultdict(int)
    for alert in alerts:
        if alert.schema == schema and (
            rule_id in alert.core_rule_ids or rule_id in alert.readiness_rule_ids
        ):
            events[alert.application] += alert.row_count
    if summary is not None and summary.inputs.facts is not None:
        events = fact(summary.inputs, schema, "rule_apps").get(rule_id, {})
    top = sorted(events.items(), key=lambda item: (-item[1], item[0]))[:TOP_RULE_APPLICATIONS]
    return ", ".join(h(app) for app, _ in top) or "—"


def _by_rule(summary: TeamSummary, rule_link: RuleLink) -> str:
    admin = summary.inputs.surface == "admin"
    rules = sorted(summary.inputs.rules, key=lambda r: (_rule_order(r.rule_id), r.schema))
    if not rules:
        return _card("rules", "Flagged by rule", "", _empty("No rule matched this week."))

    def schema_alerts(schema: str) -> list[AlertRow]:
        return [alert for alert in summary.inputs.alerts if alert.schema == schema]

    rows = []
    for rule in rules:
        kind = '<span class="chip ready">readiness</span>' if _is_readiness(rule.rule_id) else ""
        per_day = ""
        if admin:
            rate = "—" if rule.distinct_per_day is None else _dec(rule.distinct_per_day)
            per_day = f'<td class="num">{rate}</td>'
        rows.append(
            f'<tr><td><a href="{rule_link(rule.rule_id)}">{h(rule.rule_id)}</a></td>'
            f"<td>{h(_rule_title(rule.rule_id))} {kind}</td><td>{_chip(rule.schema)}</td>"
            f'<td class="num">{rule.events:,}</td><td class="num">{rule.alerts:,}</td>{per_day}'
            f"<td>{_top_applications(summary.inputs.alerts, rule.schema, rule.rule_id, summary)}</td>"
            f'<td class="step">{h(_rule_step(rule.rule_id, schema_alerts(rule.schema), summary, rule.schema))}</td></tr>'
        )
    head = ["Rule", "What it means", "Schema", "Events", "Alerts"]
    if admin:
        head.append("Distinct per day")
    head += ["Top applications", "What to change"]
    return _card(
        "rules",
        "Flagged by rule",
        "Events and distinct alerts this week. Readiness gaps are counted apart from quality.",
        _table(head, rows, "rules"),
    )


# ------------------------------------------------------------------ 10-11: visibility


def _listed(alerts: Sequence[AlertRow], totals: dict[str, int] | None = None) -> str:
    ordered = sorted(alerts, key=lambda a: (-a.row_count, a.schema, a.application, a.key_field))
    items = "".join(
        f'<li>{_alert_line(alert)}<span class="cnt">{_plural(alert.row_count, "event")}</span></li>'
        for alert in ordered[:TOP_LISTED]
    )
    more = ""
    totals = totals or {s: sum(a.schema == s for a in ordered) for s in SCHEMAS}
    if sum(totals.values()) > TOP_LISTED:
        per_schema = " and ".join(
            f"{totals[schema]:,} {schema}" for schema in SCHEMAS if totals[schema]
        )
        more = (
            f'<p class="sub">The {TOP_LISTED} with the most events; the work list has all of '
            f"them: {per_schema}.</p>"
        )
    return f'<ul class="listed">{items}</ul>{more}' if items else ""


#: The NULL-``unseen`` state. NULL means either that no panel was supplied for the schema or
#: that the week was stored before the measure existed (migration 005), and the copy has to be
#: true in both cases, so it never claims there was no dashboard.
NOT_MEASURED: Final = "Not measured this week"


def _not_measured(schema: str) -> str:
    return (
        f"<li>{_chip(schema)} <b>{NOT_MEASURED}</b> "
        f'<span class="sub">No {schema} dashboard was supplied, or the week predates this '
        "measure.</span></li>"
    )


def _hidden(summary: TeamSummary, rule_link: RuleLink) -> str:
    admin = summary.inputs.surface == "admin"
    lines = []
    for schema in SCHEMAS:
        totals = summary.inputs.schemas[schema]
        if totals.suppressed:
            text = f"<b>{_plural(totals.suppressed, 'event')}</b> hidden by a filter in your own panels"
        elif totals.unseen is None:
            # No panel, or a week stored before ``unseen`` existed: both are true of this.
            text = "No event matched a panel filter that could be checked"
        elif admin and totals.suppression_unmeasured == 0:
            text = "Nothing hidden by your own panels"
        else:
            # The portal cannot see unmeasured clauses, and an operator may have some: claim
            # only what was checked.
            text = "No event matched a panel filter that could be checked"
        if admin and totals.suppression_unmeasured:
            text += (
                f' <span class="sub">· {_plural(totals.suppression_unmeasured, "clause")} '
                "unmeasured</span>"
            )
        lines.append(f"<li>{_chip(schema)} {text}</li>")
    hidden = [a for a in summary.inputs.alerts if "R5" in a.core_rule_ids]
    hidden_totals = {
        s: sum(r.alerts for r in summary.inputs.rules if r.schema == s and r.rule_id == "R5")
        for s in SCHEMAS
    }
    fix = (
        f'<div class="next"><b>Next step:</b> {h(_next_step("R5"))}</div>'
        f'<a class="more" href="{rule_link("R5")}">Show these alerts</a>'
        if hidden
        else ""
    )
    return _card(
        "hidden",
        "Hidden by your own panels",
        "Alerts you still send that a filter in every one of your dashboards hides (R5).",
        f'<ul class="vis">{"".join(lines)}</ul>{_listed(hidden, hidden_totals)}{fix}',
    )


def _unseen(summary: TeamSummary) -> str:
    admin = summary.inputs.surface == "admin"
    lines = []
    for schema in SCHEMAS:
        totals: SchemaTotals = summary.inputs.schemas[schema]
        if totals.unseen is None:
            lines.append(_not_measured(schema))
            continue
        if totals.unseen:
            verb = "reaches" if totals.unseen == 1 else "reach"
            text = (
                f"<b>{_plural(totals.unseen, 'event')}</b> from "
                f"{_plural(totals.unseen_alerts or 0, 'alert')} {verb} none of your dashboards"
            )
        else:
            text = "No alert falls outside every panel's narrowing."
        if admin and totals.unseen_unmeasured:
            text += (
                f' <span class="sub">· {_plural(totals.unseen_unmeasured, "clause")} '
                "unmeasured</span>"
            )
        lines.append(f"<li>{_chip(schema)} {text}</li>")
    unseen = [a for a in summary.inputs.alerts if a.unseen]
    unseen_totals = {s: summary.inputs.schemas[s].unseen_alerts or 0 for s in SCHEMAS}
    return _card(
        "unseen",
        "Not on any of your dashboards",
        "Alerts you own that no panel's narrowing includes, so on-call never sees them. "
        "Counted apart from the hidden ones.",
        f'<ul class="vis">{"".join(lines)}</ul>{_listed(unseen, unseen_totals)}',
    )


# ------------------------------------------------------------------ 12: migration progress


def _estimate(estimate: Estimate) -> str:
    caveats = (
        '<ul class="caveats">'
        "<li>Both figures are projections, not commitments.</li>"
        "<li>v1 falling may be cleanup rather than migration.</li>"
        "<li>Teams rebuild rather than port, so v1 can fall while monitoring is lost.</li>"
        "</ul>"
    )
    if estimate.rules_left == 0:
        pace = (
            '<p class="big">Phase 1 has no v1 alert rules left</p>'
            '<p class="sub">No v1 alert fired this week.</p>'
        )
    elif estimate.projected_week_end is not None:
        pace_value = estimate.pace_per_week or 0.0
        inputs = [
            f"{_plural(estimate.rules_left, 'v1 alert rule')} fired this week",
            f"{_plural(estimate.retired or 0, 'rule')} stopped firing across the "
            f"{_plural(estimate.lookback_weeks, 'earlier published week')}",
            f"pace: {_dec(pace_value)} {'rule' if pace_value == 1 else 'rules'} a week",
        ]
        pace = (
            f'<p class="big">{h(format_projected_week(estimate.projected_week_end))}</p>'
            '<p class="sub">when the last v1 alert rule would stop firing at this pace</p>'
            f'<ul class="inputs">{"".join(f"<li>{h(i)}</li>" for i in inputs)}</ul>'
        )
    else:
        pace = (
            f'<p class="noest"><b>No estimate:</b> {h(estimate.no_estimate_reason or "")}</p>'
            f'<ul class="inputs"><li>{h(_plural(estimate.rules_left, "v1 alert rule"))} fired '
            "this week</li></ul>"
        )
    source = "set for this team" if estimate.effort_is_override else "default"
    effort = (
        f'<p class="big">{_dec(estimate.effort_days)} working days</p>'
        f'<p class="sub">about {_dec(estimate.effort_weeks)} working weeks · '
        "configured, not measured</p>"
        '<ul class="inputs">'
        f"<li>{h(_plural(estimate.rules_left, 'v1 alert rule'))} {TIMES} "
        f"{_dec(estimate.effort_days_per_rule)} working days each ({source})</li>"
        "<li>Rebuilding each rule in v2 and re-deciding its severity. Impact and runbook are "
        "phase 2 and not included.</li></ul>"
    )
    return (
        '<div class="two est">'
        '<div class="estc"><div class="eyebrow">Projection · measured pace</div>'
        f"{pace}</div>"
        '<div class="estc"><div class="eyebrow">Projection · configured effort</div>'
        f"{effort}</div>"
        f"</div>{caveats}"
    )


def _progress(summary: TeamSummary) -> str:
    inputs = summary.inputs
    steps = "".join(
        f'<li class="{"on" if key == inputs.phase else ""}"><b>{h(label)}</b>'
        f'<span class="sub">Done when {h(DONE_WHEN[key])}</span></li>'
        for key, label in PHASE_STEPS
    )
    v1 = [a for a in inputs.alerts if a.schema == "v1"]
    v2 = [a for a in inputs.alerts if a.schema == "v2"]
    left = (
        '<dl class="facts">'
        f'<dt>v1 alerts</dt><dd class="num">{inputs.schemas["v1"].distinct_alerts:,}</dd>'
        f'<dt>v1 applications</dt><dd class="num">{fact(inputs, "v1", "applications", len({a.application for a in v1})):,}</dd>'
        f'<dt>v1 alert rules</dt><dd class="num">{summary.estimate.rules_left:,}</dd>'
        "</dl>"
    )

    def ready(alert: AlertRow) -> bool:
        gaps = set(alert.readiness_rule_ids)
        critical = (alert.severity or "").lower() == "critical"
        return not ({"R8", "R10"} & gaps) and not (critical and "R9" in gaps)

    critical_no_runbook = sum(
        1 for a in v2 if (a.severity or "").lower() == "critical" and "R9" in a.readiness_rule_ids
    )
    critical_no_runbook = fact(inputs, "v2", "critical", critical_no_runbook)
    readiness = (
        '<dl class="facts">'
        f'<dt>Phase-2 ready</dt><dd class="num">{fact(inputs, "v2", "ready", sum(ready(a) for a in v2)):,} of '
        f"{inputs.schemas['v2'].distinct_alerts:,} v2 alerts</dd>"
        f'<dt>Critical without a runbook</dt><dd class="num">{critical_no_runbook:,}</dd>'
        "</dl>"
        if v2
        else _empty("No v2 alerts this week.")
    )
    return _card(
        "progress",
        "Migration progress",
        "Where the move to v2 stands this week, and how long phase 1 may take.",
        f'<ol class="phases">{steps}</ol>'
        '<div class="two">'
        f'<div><div class="eyebrow">Left in phase 1</div>{left}</div>'
        f'<div><div class="eyebrow">Phase-2 readiness</div>{readiness}</div>'
        "</div>"
        f"{_estimate(summary.estimate)}",
    )


# ------------------------------------------------------------------ the whole summary


def render_summary_sections(
    summary: TeamSummary,
    *,
    rule_link: Callable[[str | None], str],
) -> str:
    """Every shared Summary widget as HTML, surface-aware via summary.inputs.surface.

    rule_link(rule_id) returns the escaped href of the work list filtered by that rule
    (None = unfiltered). No <script>, no style= attributes. The portal surface never
    prints per-day rates or internals; the admin surface prints distinct_per_day when set.
    """
    return (
        '<div class="summary">'
        f'<div class="two">{_glance(summary, "v1")}{_glance(summary, "v2")}</div>'
        f'<div class="two">{_why(summary, rule_link)}{_key_findings(summary, rule_link)}</div>'
        f"{_by_application(summary, rule_link)}"
        f"{_fire(summary)}"
        f"{_biggest(summary, rule_link)}"
        f"{_by_rule(summary, rule_link)}"
        f'<div class="two">{_hidden(summary, rule_link)}{_unseen(summary)}</div>'
        f"{_progress(summary)}"
        f"{render_slides(summary)}"
        "</div>"
    )
