"""Two ready-made 16:9 presentation slides at the bottom of the team Summary.

The standardization team presents each team's week in PowerPoint. Each frame here is exactly
1280 x 720 CSS px, so a screenshot of it pastes as a slide with nothing to rearrange. Slide 1
says where the team stands; slide 2 shows the week day by day, one chart per schema.

Same rules as the rest of the Summary (:mod:`alerts_bi_shared.ui.summary_view`): built only from the
:class:`~alerts_bi_shared.insights.TeamSummary` already computed, every value escaped through
:func:`~alerts_bi_shared.ui.html.h`, no script, no inline ``style`` (bars are SVG geometry coloured by
CSS classes), no link, and v1 and v2 never added together. Frames always use a light palette,
even in dark mode, because they are made to be projected; the stylesheet scopes those colours
to ``.slide``. Lists are capped and padded so the layout holds still from week to week, and
long text is cut rather than allowed to spill out of the frame.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from alerts_bi_shared.insights import (
    AlertRow,
    AppRow,
    DailyPoint,
    Estimate,
    SchemaTotals,
    TeamSummary,
)
from alerts_bi_shared.ui.charts import nice_step
from alerts_bi_shared.ui.explain import EN_DASH
from alerts_bi_shared.ui.html import PHASE_STEPS, SCHEMA_NAMES, h

__all__ = ["percent", "render_slides"]

SCHEMAS = ("v1", "v2")
#: Caps, so a busy week cannot push content out of the frame.
TOP_FINDINGS = 3
TOP_APPLICATIONS = 3
MESSAGE_LIMIT = 90
#: The R6 firing patterns, in the order the slide lists them.
PATTERNS = ("stuck", "spamming", "flapping")
ELLIPSIS = "…"
DASH = "—"

#: The examined split: always these three, then the other states only when non-zero.
_SPLIT = (
    ("rule_flagged", "sl-q-rule", "rule-flagged", True),
    ("llm_flagged", "sl-q-model", "model-flagged (advisory)", True),
    ("assessed_good", "sl-q-good", "assessed good", True),
    ("needs_review", "sl-q-review", "needs a decision", False),
    ("unassessed", "sl-q-un", "not reviewed", False),
)


# ------------------------------------------------------------------ small helpers


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def _clip(text: str, limit: int) -> str:
    """At most ``limit`` characters, the last one an ellipsis when anything was cut."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + ELLIPSIS


def percent(part: float, whole: float) -> str:
    """``part`` as a share of ``whole``, the one way every percentage on the slides is written.

    Rounds half up exactly (``12.5`` is ``13%``), never to even. A share that is not nothing
    never reads ``0%``: it reads ``<1%``. And only the whole reads ``100%``: anything short
    of it stops at ``99%``, so a team 99.6% ready is not shown as done.
    """
    if whole <= 0 or part <= 0:
        return "0%"
    if part >= whole:
        return "100%"
    share = Decimal(str(part)) * 100 / Decimal(str(whole))
    rounded = int(share.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if rounded < 1:
        return "<1%"
    return f"{min(rounded, 99)}%"


def _day(moment: datetime | date) -> str:
    """``28 Sep 2026``."""
    return f"{moment.day} {moment:%b %Y}"


def _chip(schema: str) -> str:
    return f'<span class="sl-chip {h(schema)}">{h(schema)}</span>'


def _phase_label(phase: str) -> str:
    for key, label in PHASE_STEPS:
        if key == phase:
            return label
    return "No alerts this week" if phase == "no_data" else phase


def _block(css: str, title: str, body: str) -> str:
    return f'<div class="sl-block {css}"><h5 class="sl-label">{h(title)}</h5>{body}</div>'


def _none() -> str:
    return '<p class="sl-none">None this week</p>'


def _footer(summary: TeamSummary, number: int) -> str:
    inputs = summary.inputs
    scope = (
        " · Applications: " + (", ".join(inputs.applications) or "none selected")
        if inputs.applications is not None
        else ""
    )
    return (
        '<footer class="sl-foot">'
        f"<span>{h(inputs.display_name)} · week ending {h(_day(inputs.window_end))} · "
        f"Alerts BI{h(scope)}</span>"
        f'<span class="sl-page">{number} / 2</span></footer>'
    )


def _frame(number: int, title: str, head_extra: str, body: str, summary: TeamSummary) -> str:
    return (
        f'<section class="slide sl-{number}" aria-label="Slide {number}: {h(title)}">'
        f'<header class="sl-head"><h4 class="sl-title">{h(title)}</h4>{head_extra}</header>'
        f"{body}{_footer(summary, number)}</section>"
    )


# ------------------------------------------------------------------ slide 1


def _split_bar(totals: SchemaTotals) -> str:
    shown = [
        (css, label, int(totals.states.get(state, 0)))
        for state, css, label, always in _SPLIT
        if always or totals.states.get(state, 0)
    ]
    total = sum(count for _, _, count in shown)
    rects, offset = [], 0.0
    for css, label, count in shown:
        if not count:
            continue
        width = 100 * count / total
        rects.append(
            f'<rect class="{css}" x="{offset:.3f}" y="0" width="{max(width - 0.3, 0.2):.3f}" '
            f'height="4"><title>{h(label)}: {count:,}</title></rect>'
        )
        offset += width
    legend = "".join(
        f'<li><span class="sl-sw {css}" aria-hidden="true"></span>{h(label)} <b>{count:,}</b></li>'
        for css, label, count in shown
    )
    return (
        f'<svg class="sl-bar" viewBox="0 0 100 4" preserveAspectRatio="none" role="img" '
        f'aria-label="{h(totals.schema)} alerts by review outcome">'
        f'<rect class="sl-track" x="0" y="0" width="100" height="4"/>{"".join(rects)}</svg>'
        f'<ul class="sl-legend">{legend}</ul>'
    )


def _schema_panel(totals: SchemaTotals, schema: str) -> str:
    heading = f'<h5 class="sl-schema-h">{_chip(schema)}{h(SCHEMA_NAMES[schema])}</h5>'
    if totals.events == 0 and totals.distinct_alerts == 0:
        return (
            f'<div class="sl-schema {schema} sl-empty">{heading}'
            f'<p class="sl-none">No {h(schema)} alerts this week</p></div>'
        )
    flagged = totals.rule_flagged_alerts
    share = (
        "(event share unavailable)"
        if totals.rule_flagged_events is None
        else f"({percent(totals.rule_flagged_events, totals.events)} of events)"
    )
    return (
        f'<div class="sl-schema {schema}">{heading}'
        '<div class="sl-kpi">'
        f'<p class="sl-big"><b>{totals.distinct_alerts:,}</b>'
        f"<span>{'alert' if totals.distinct_alerts == 1 else 'alerts'}</span></p>"
        '<ul class="sl-facts">'
        f"<li>{_plural(totals.events, 'event')}</li>"
        f"<li>{flagged:,} rule-flagged {'alert' if flagged == 1 else 'alerts'} "
        f"{h(share)}</li>"
        "</ul></div>"
        f"{_split_bar(totals)}</div>"
    )


def _findings(summary: TeamSummary) -> str:
    findings = summary.key_findings[:TOP_FINDINGS]
    if not findings:
        return _block("sl-kf", "Key findings", _none())
    items = [
        f"<li><b>{h(finding.title)}</b> <span>{h(finding.body)}</span></li>" for finding in findings
    ]
    items += ['<li class="sl-pad">' + DASH + "</li>"] * (TOP_FINDINGS - len(items))
    return _block("sl-kf", "Key findings", f'<ol class="sl-lines">{"".join(items)}</ol>')


BIGGEST_TITLE = "Biggest single alert"
BIGGEST_DEFINITION = "Based on the alert identity: the application field plus the alert key."


def _biggest(summary: TeamSummary) -> str:
    alert: AlertRow | None = summary.biggest
    definition = f'<p class="sl-def">{h(BIGGEST_DEFINITION)}</p>'
    if alert is None:
        return _block("sl-big1", BIGGEST_TITLE, definition + _none())
    schema_totals = summary.inputs.schemas.get(alert.schema)
    total = max(schema_totals.events if schema_totals else 0, alert.row_count)
    message = _clip(alert.message or "(no message)", MESSAGE_LIMIT)
    return _block(
        "sl-big1",
        BIGGEST_TITLE,
        f"{definition}"
        f'<p class="sl-lead">1 alert produced <b>{alert.row_count:,}</b> of {total:,} '
        f"{h(alert.schema)} events ({h(percent(alert.row_count, total))})</p>"
        f'<p class="sl-app">Application: <b>{h(alert.application)}</b></p>'
        f'<p class="sl-msg">Message: “{h(message)}”</p>',
    )


def _slide_one(summary: TeamSummary) -> str:
    inputs = summary.inputs
    start, end = inputs.window_start, inputs.window_end
    # A week with no alerts has no readiness to speak of, whatever the stored value.
    readiness = (
        f"Phase-2 readiness: {DASH}"
        if inputs.phase2_readiness_pct is None or inputs.phase == "no_data"
        else f"{percent(inputs.phase2_readiness_pct, 100)} phase-2 ready"
    )
    subtitle = (
        '<p class="sl-sub">'
        f"<span>Week of {start.day} {start:%b} {EN_DASH} {h(_day(end))} (UTC)</span>"
        f'<span class="sl-phase">{"Team: " if inputs.applications is not None else ""}{h(_phase_label(inputs.phase))}</span>'
        f"<span>{'Team: ' if inputs.applications is not None else ''}{h(readiness)}</span></p>"
    )
    panels = "".join(_schema_panel(inputs.schemas[schema], schema) for schema in SCHEMAS)
    body = (
        f'<div class="sl-schemas">{panels}</div>'
        f'<div class="sl-row">{_findings(summary)}{_biggest(summary)}</div>'
    )
    return _frame(1, f"Where {inputs.display_name} stands", subtitle, body, summary)


# ------------------------------------------------------------------ slide 2


#: The chart's drawing box in CSS px: the SVG is drawn 1:1, so text sizes are real sizes.
CHART_W, CHART_H = 576, 170
_LEFT, _RIGHT, _TOP, _BOTTOM = 72, 64, 10, 34
#: Two fixed categorical slots, validated together on the white slide surface. Distinct
#: alerts are drawn first and wide, rule-flagged on top, thin and dashed, with a smaller
#: marker: the noisy line often equals the distinct one, and both must stay visible
#: without relying on colour alone. The radius is each series' marker size.
SERIES = (
    ("distinct", "Distinct alerts", "sl-s1", 6),
    ("flagged", "Rule-flagged (noisy)", "sl-s2", 4),
)
PARTIAL_NOTE = "Hollow points are partial days (the week does not start at midnight UTC)."


def _is_partial(point: DailyPoint) -> bool:
    return point.covered_hours < 24


def _scale(maximum: int) -> tuple[int, int]:
    """A whole-number tick step and the axis top: 3 or 4 gridlines, starting at 0."""
    step = max(1, math.ceil(nice_step(maximum / 3))) if maximum > 0 else 1
    top = max(2 * step, math.ceil(maximum / step) * step)
    return step, top


def _value(point: DailyPoint, key: str) -> int:
    return point.distinct_alerts if key == "distinct" else point.rule_flagged_distinct


def _chart_svg(schema: str, points: Sequence[DailyPoint]) -> str:
    plot_w = CHART_W - _LEFT - _RIGHT
    base = CHART_H - _BOTTOM
    step, top = _scale(max(max(p.distinct_alerts, p.rule_flagged_distinct) for p in points))

    def x(i: int) -> float:
        return _LEFT + plot_w / 2 if len(points) == 1 else _LEFT + i * plot_w / (len(points) - 1)

    def y(value: float) -> float:
        return base - value / top * (base - _TOP)

    parts = []
    for tick in range(0, top + 1, step):
        parts.append(
            f'<line class="sl-grid" x1="{_LEFT}" x2="{CHART_W - _RIGHT}" '
            f'y1="{y(tick):.1f}" y2="{y(tick):.1f}"/>'
            f'<text class="sl-tick" x="{_LEFT - 10}" y="{y(tick) + 6:.1f}" '
            f'text-anchor="end">{tick:,}</text>'
        )
    for i, point in enumerate(points):
        parts.append(
            f'<text class="sl-tick" x="{x(i):.1f}" y="{CHART_H - 4}" text-anchor="middle">'
            f"{point.day:%a} {point.day.day}</text>"
        )
    ends = []
    for key, label, css, radius in SERIES:
        values = [_value(p, key) for p in points]
        path = " ".join(
            f"{'M' if i == 0 else 'L'}{x(i):.1f} {y(v):.1f}" for i, v in enumerate(values)
        )
        parts.append(f'<path class="sl-line {css}" d="{path}"/>')
        for i, (point, value) in enumerate(zip(points, values, strict=True)):
            hollow = " sl-hollow" if _is_partial(point) else ""
            parts.append(
                f'<circle class="sl-pt {css}{hollow}" cx="{x(i):.1f}" cy="{y(value):.1f}" r="{radius}">'
                f"<title>{h(label)}, {point.day:%a} {point.day.day}: {value:,}</title></circle>"
            )
        ends.append(y(values[-1]))
    # Direct-label the last value of each line only. Distinct alerts never sit below the
    # rule-flagged count, so when the two ends meet the labels part above and below, and
    # the lower one stays clear of the baseline and the day labels under it.
    upper, lower = ends
    if lower - upper < 22:
        middle = (upper + lower) / 2
        upper, lower = middle - 11, middle + 11
    lowest = base - 10
    if lower > lowest:
        upper, lower = min(upper, lowest - 22), lowest
    for (key, _label, _css, _radius), label_y in zip(SERIES, (upper, lower), strict=True):
        parts.append(
            f'<text class="sl-end" x="{x(len(points) - 1) + 10:.1f}" y="{label_y + 6:.1f}">'
            f"{_value(points[-1], key):,}</text>"
        )
    return (
        f'<svg class="sl-chart" viewBox="0 0 {CHART_W} {CHART_H}" role="img" '
        f'aria-label="{h(schema)} distinct alerts and rule-flagged alerts by UTC day">'
        f"{''.join(parts)}</svg>"
    )


def _fire_line(summary: TeamSummary, schema: str) -> str:
    from alerts_bi_shared.insights.stored import patterns

    counts = [patterns(summary.inputs, schema).get(pattern, [0])[0] for pattern in PATTERNS]
    text = " · ".join(
        f"{count:,} {pattern}" for count, pattern in zip(counts, PATTERNS, strict=True)
    )
    return f'<p class="sl-fire-line">Firing patterns · {h(schema)}: {text}</p>'


def _charted(summary: TeamSummary, schema: str) -> list[DailyPoint]:
    """The schema's day buckets in stored order, or nothing when there is nothing to draw."""
    points = [p for p in summary.inputs.daily if p.alert_schema == schema]
    if any(p.distinct_alerts or p.rule_flagged_distinct for p in points):
        return points
    return []


def _legend() -> str:
    items = "".join(
        f'<li><svg class="sl-key" viewBox="0 0 28 8" aria-hidden="true">'
        f'<path class="sl-key-l {css}" d="M0 4H28"/></svg>{h(label)}</li>'
        for _key, label, css, _radius in SERIES
    )
    return f'<ul class="sl-chart-legend">{items}</ul>'


def _chart(summary: TeamSummary, schema: str) -> str:
    points = _charted(summary, schema)
    if (
        summary.inputs.applications is not None
        and not points
        and summary.inputs.schemas[schema].distinct_alerts
    ):
        body = '<p class="sl-chart-empty">Application breakdown unavailable</p>'
    elif points:
        body = _legend() + _chart_svg(schema, points)
    else:
        # The box takes the legend's place too, so both charts keep one height.
        body = f'<p class="sl-chart-empty">No {h(schema)} alerts this week</p>'
    return (
        f'<div class="sl-block sl-chartbox {schema}">'
        f'<h5 class="sl-chart-h">{h(schema)}: distinct alerts by UTC day</h5>'
        f"{body}{_fire_line(summary, schema)}</div>"
    )


def _app(row: AppRow) -> str:
    return (
        "<li>"
        f'<span class="sl-an">{h(row.application)}</span>{_chip(row.schema)}'
        f'<span class="sl-ae">{_plural(row.events, "event")}</span>'
        f'<span class="sl-af"><b>{row.rule_flagged_alerts:,}</b> rule-flagged · '
        f"<b>{row.llm_flagged_alerts:,}</b> model (advisory)</span></li>"
    )


def _applications(summary: TeamSummary) -> str:
    apps = summary.by_application[:TOP_APPLICATIONS]
    if not apps:
        return _block("sl-apps", "Noisiest applications", _none())
    items = [_app(row) for row in apps]
    items += ['<li class="sl-pad">' + DASH + "</li>"] * (TOP_APPLICATIONS - len(items))
    return _block(
        "sl-apps", "Noisiest applications", f'<ol class="sl-app-list">{"".join(items)}</ol>'
    )


def _not_consumed_tile(summary: TeamSummary, schema: str) -> str:
    # The headline is distinct alerts, not events: Grafana writes a row on every evaluation,
    # so an event count reflects the evaluation cadence more than the alert, while distinct
    # alerts are what the charts above count. Events stay beside it as the secondary line.
    totals = summary.inputs.schemas[schema]
    filtered = sum(
        r.alerts for r in summary.inputs.rules if r.schema == schema and r.rule_id == "R5"
    )
    if (
        totals.unseen is None
        and totals.unseen_alerts is None
        and not filtered
        and not totals.suppressed
    ):
        # Nothing was measured: no panel was supplied for this schema, or the week was
        # published before `unseen` existed and its panels filtered nothing. A "0" here would
        # say the team's panel hides nothing when no panel was read (design 3.2), and "no
        # dashboard supplied" could be false for an older week. Suppression itself predates
        # `unseen`, so an older week whose panels did filter alerts still shows its count.
        return (
            f'<div class="sl-nc {schema}">'
            f'<p class="sl-nc-n">{_chip(schema)}<b>{DASH}</b></p>'
            '<p class="sl-na">not measured this week</p></div>'
        )
    unseen = (
        '<span class="sl-na">not measured this week</span>'
        if totals.unseen_alerts is None
        else f"{_plural(totals.unseen_alerts or 0, 'alert')} on no dashboard"
    )
    return (
        f'<div class="sl-nc {schema}">'
        f'<p class="sl-nc-n">{_chip(schema)}<b>{filtered:,}</b></p>'
        f'<p class="sl-nc-u">{"alert" if filtered == 1 else "alerts"} filtered out by your '
        "panel SQL</p>"
        f"<p>{_plural(totals.suppressed, 'event')}</p><p>{unseen}</p></div>"
    )


def _not_consumed(summary: TeamSummary) -> str:
    tiles = "".join(_not_consumed_tile(summary, schema) for schema in SCHEMAS)
    return _block(
        "sl-ncb", "Not consumed by your dashboards", f'<div class="sl-nc-row">{tiles}</div>'
    )


def _g(value: float) -> str:
    return f"{value:g}"


def _time_left(estimate: Estimate) -> str:
    if estimate.projected_week_end is not None:
        when = f'<p class="sl-when">week of {h(_day(estimate.projected_week_end))}</p>'
    else:
        when = (
            f'<p class="sl-noest"><b>No estimate:</b> {h(estimate.no_estimate_reason or DASH)}</p>'
        )
    return _block(
        "sl-time",
        "Time to finish phase 1",
        f"{when}"
        f"<p>{h(_plural(estimate.rules_left, 'v1 alert rule'))} left</p>"
        f'<p class="sl-effort">≈ {_g(estimate.effort_days)}'
        f" working {'day' if estimate.effort_days == 1 else 'days'} "
        f"({estimate.effort_weeks:.1f} weeks) at "
        f"{_g(estimate.effort_days_per_rule)} days per rule, configured</p>"
        '<p class="sl-note">A projection. v1 falling may be cleanup rather than migration.</p>',
    )


def _slide_two(summary: TeamSummary) -> str:
    charts = "".join(_chart(summary, schema) for schema in SCHEMAS)
    # Only drawn points can be hollow, so the footnote follows the charts actually shown.
    partial = any(_is_partial(p) for schema in SCHEMAS for p in _charted(summary, schema))
    note = f'<p class="sl-partial">{h(PARTIAL_NOTE)}</p>' if partial else ""
    body = (
        f'<div class="sl-charts">{charts}</div>{note}'
        f'<div class="sl-bottom">{_not_consumed(summary)}{_applications(summary)}'
        f"{_time_left(summary.estimate)}</div>"
    )
    return _frame(2, "The week, day by day", "", body, summary)


# ------------------------------------------------------------------ the section


def render_slides(summary: TeamSummary) -> str:
    """The "Presentation" section: two fixed 1280 x 720 frames, ready to screenshot."""
    return (
        '<section class="slides" aria-labelledby="slides-h">'
        '<div class="sl-intro"><h3 id="slides-h">Presentation</h3>'
        "<p>Two 16:9 slides for this week. Screenshot each frame and paste it as a slide.</p>"
        "</div>"
        f'<div class="sl-scroll">{_slide_one(summary)}{_slide_two(summary)}</div>'
        "</section>"
    )
