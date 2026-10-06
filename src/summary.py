"""The operator Summary page: one completed run of one team, internals included (spec 4, 7).

The admin app reads base tables, so this module turns the run's own rows into the shared
:class:`~alerts_bi_shared.insights.SummaryInputs` (``surface="admin"``) and adds what only operators see:
the run strip, the publication state, the schedule's last outcome, a day-by-day chart, each
panel's frozen SQL with its suppression clauses marked, and a filterable work list paged in
SQL.

The time-to-v2 estimate reads **published weeks only**, exactly as the portal does, so an
operator and a reader always see the same figure; an unpublished run gets none.

No script and no inline ``style``: bars and charts are SVG geometry and CSS classes, and
filtering is a GET form, so the admin Content-Security-Policy holds.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final, Literal
from urllib.parse import quote, urlencode

from alerts_bi_operations.db.reports import get_daily_metrics, get_rule_counts, get_run_panels
from alerts_bi_operations.suppression.fields import classify_field
from alerts_bi_operations.suppression.lexer import SqlParseError, Token, tokenize
from alerts_bi_operations.suppression.parser import Leaf, collect_leaves, parse_panel_sql
from alerts_bi_shared.db.connection import Database
from alerts_bi_shared.insights import AlertRow, RuleTotal, SchemaTotals, SummaryInputs, WeekRules
from alerts_bi_shared.insights.daily import daily_points
from alerts_bi_shared.insights.estimate import v1_rule_key_of
from alerts_bi_shared.insights.summary import summarize
from alerts_bi_shared.ui.charts import ChartPoint, line_chart
from alerts_bi_shared.ui.explain import (
    EN_DASH,
    QUALITY_STATE_LABELS,
    format_date,
    format_instant,
    format_week,
)
from alerts_bi_shared.ui.html import h
from alerts_bi_shared.ui.summary_view import render_summary_sections
from alerts_bi_shared.window import WINDOW_DAYS

from . import pages

__all__ = [
    "PAGE_SIZE",
    "RULE_PATTERN",
    "SORTS",
    "STATES",
    "AdminSummary",
    "Publication",
    "SnapshotEntry",
    "SnapshotPanel",
    "SummaryNotFound",
    "WorklistFilter",
    "alert_row",
    "basis_changes",
    "day_by_day",
    "highlight_panel_sql",
    "load_admin_summary",
    "measurement_basis",
    "read_snapshot",
    "rule_totals",
    "schema_totals",
    "shared_sections",
    "summary_inputs",
    "summary_page",
    "summary_url",
    "worklist_where",
]

PAGE_SIZE: Final = 50
SCHEMAS: Final = ("v1", "v2")
#: Work-list quality-state filter values; ``all`` means no filter.
STATES: Final = (
    "all",
    "rule_flagged",
    "llm_flagged",
    "needs_review",
    "assessed_good",
    "unassessed",
)
#: A rule filter is one deterministic rule id, or empty for none.
RULE_PATTERN: Final = "^(R(10|[1-9]))?$"
#: Work-list orders. ``events``: the noisiest alerts first. ``application``: grouped by
#: application. Each ends on the identity, so paging is stable.
SORTS: Final = {
    "events": "row_count DESC, application, key_field, alert_schema",
    "application": "application, alert_schema, key_field",
}

#: The ``alert_findings`` columns an :class:`AlertRow` needs. The representative document and
#: the evidence stay in SQL: the summary never shows them.
_ALERT_COLUMNS: Final = (
    "alert_schema, application, key_field, message, severity, provider, alert_rule_url, "
    "component, node_name, row_count, first_seen, last_seen, quality_state, core_rule_ids, "
    "readiness_rule_ids, llm_principle_id, llm_confidence, clear_count, max_clear_cycles_24h, "
    "fire_pattern, unseen, max_episode_firing_rows, open_since"
)


class SummaryNotFound(LookupError):
    """No such team, run, or completed run of that team: the page answers 404."""


# ------------------------------------------------------------------ shapes


@dataclass(frozen=True, slots=True)
class WorklistFilter:
    state: str = "all"
    schema: str = "all"
    rule: str = ""
    sort: str = "events"
    page: int = 1


@dataclass(frozen=True, slots=True)
class Publication:
    """The selected run's publication: currently published, withdrawn, or never published."""

    state: Literal["published", "withdrawn", "never"]
    by: str | None = None
    at: datetime | None = None
    note: str | None = None
    """The review note when published, the withdrawal reason when withdrawn."""


@dataclass(frozen=True, slots=True)
class SnapshotPanel:
    panel_id: str
    schema: str
    sql: str


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    """What the run's stored registry entry says, read leniently: it is shown, not trusted."""

    v1_operators: tuple[str, ...] = ()
    v2_operator: str | None = None
    panels: tuple[SnapshotPanel, ...] = ()
    v1_rule_effort_days: float | None = None


@dataclass(frozen=True, slots=True)
class AdminSummary:
    run: Mapping[str, Any]
    inputs: SummaryInputs
    entry: SnapshotEntry
    daily: Sequence[Mapping[str, Any]]
    stored_panels: Sequence[Mapping[str, Any]]
    """``run_panels`` rows: what the run measured from each panel."""
    runs: Sequence[Mapping[str, Any]]
    """The team's completed runs, for the picker."""
    publication: Publication
    schedule: Mapping[str, Any] | None
    worklist: tuple[AlertRow, ...]
    total: int
    filters: WorklistFilter


# ------------------------------------------------------------------ rows to inputs


def _ids(value: Any) -> tuple[str, ...]:
    return tuple(part.strip() for part in str(value or "").split(",") if part.strip())


def _optional(value: Any) -> str | None:
    return None if value is None else str(value)


def alert_row(row: Mapping[str, Any]) -> AlertRow:
    unseen = row.get("unseen")
    return AlertRow(
        schema=str(row["alert_schema"]),
        application=str(row["application"]),
        key_field=str(row["key_field"]),
        message=_optional(row.get("message")),
        severity=_optional(row.get("severity")),
        provider=_optional(row.get("provider")),
        alert_rule_url=_optional(row.get("alert_rule_url")),
        component=_optional(row.get("component")),
        node_name=_optional(row.get("node_name")),
        row_count=int(row["row_count"]),
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        quality_state=str(row["quality_state"]),
        core_rule_ids=_ids(row.get("core_rule_ids")),
        readiness_rule_ids=_ids(row.get("readiness_rule_ids")),
        llm_principle_id=_optional(row.get("llm_principle_id")),
        llm_confidence=_optional(row.get("llm_confidence")),
        clear_count=int(row.get("clear_count") or 0),
        max_clear_cycles_24h=int(row.get("max_clear_cycles_24h") or 0),
        fire_pattern=_optional(row.get("fire_pattern")),
        unseen=None if unseen is None else bool(unseen),
        max_episode_firing_rows=int(row.get("max_episode_firing_rows") or 0),
        open_since=row.get("open_since"),
    )


def _sum_or_none(values: Sequence[Any]) -> int | None:
    """NULL stays NULL until something measured it: no panel, nothing claimed."""
    measured = [int(value) for value in values if value is not None]
    return sum(measured) if measured else None


def schema_totals(
    schema: str, daily: Sequence[Mapping[str, Any]], alerts: Sequence[AlertRow]
) -> SchemaTotals:
    days = [day for day in daily if day["alert_schema"] == schema]
    mine = [alert for alert in alerts if alert.schema == schema]
    states = Counter(alert.quality_state for alert in mine)
    flags = [alert.unseen for alert in mine if alert.unseen is not None]
    return SchemaTotals(
        schema=schema,
        events=sum(int(day["alerts"]) for day in days),
        distinct_alerts=len(mine),
        distinct_per_day=sum(int(day["distinct_alerts"]) for day in days) / WINDOW_DAYS,
        rule_flagged_events=sum(int(day["flagged_by_rule"]) for day in days),
        rule_flagged_alerts=states["rule_flagged"],
        suppressed=sum(int(day["suppressed"]) for day in days),
        unseen=_sum_or_none([day.get("unseen") for day in days]),
        unseen_alerts=sum(1 for flag in flags if flag) if flags else None,
        states=dict(sorted(states.items())),
        readiness_gaps=sum(1 for alert in mine if alert.readiness_rule_ids),
        suppression_unmeasured=sum(int(day.get("suppression_unmeasured") or 0) for day in days),
        unseen_unmeasured=_sum_or_none([day.get("unseen_unmeasured") for day in days]),
    )


def _rule_order(rule_id: str) -> tuple[int, str]:
    number = rule_id[1:]
    return (int(number), rule_id) if rule_id[:1] == "R" and number.isdigit() else (999, rule_id)


def rule_totals(
    rule_counts: Sequence[Mapping[str, Any]], alerts: Sequence[AlertRow]
) -> tuple[RuleTotal, ...]:
    """Per schema and rule: matching rows, alerts carrying it, and the per-day distinct rate."""
    events: dict[tuple[str, str], int] = defaultdict(int)
    distinct: dict[tuple[str, str], int] = defaultdict(int)
    for count in rule_counts:
        key = (str(count["alert_schema"]), str(count["rule_id"]))
        events[key] += int(count["match_count"])
        distinct[key] += int(count["distinct_count"])
    carriers: Counter[tuple[str, str]] = Counter(
        (alert.schema, rule)
        for alert in alerts
        for rule in {*alert.core_rule_ids, *alert.readiness_rule_ids}
    )
    return tuple(
        RuleTotal(
            schema=schema,
            rule_id=rule_id,
            events=events[(schema, rule_id)],
            alerts=carriers[(schema, rule_id)],
            distinct_per_day=distinct[(schema, rule_id)] / WINDOW_DAYS,
        )
        for schema, rule_id in sorted(events, key=lambda key: (key[0], _rule_order(key[1])))
    )


def _fragment(value: Any) -> str | None:
    """A JSON array or object as compact text, which is what SQL Server's ``JSON_QUERY``
    returns from a snapshot ``snapshot_team_entry`` wrote compactly; ``None`` otherwise."""
    if isinstance(value, list | dict):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return None


def _scalar(value: Any) -> str | None:
    """A JSON scalar as ``JSON_VALUE`` returns it; ``None`` for null, absent or a container."""
    if value is None or isinstance(value, list | dict):
        return None
    return value if isinstance(value, str) else json.dumps(value)


def measurement_basis(snapshot: Any) -> tuple[str | None, str | None, str | None]:
    """What in a team's stored registry entry changes how its alerts are measured: its v1
    operators, its v2 operator and its panels (SQL and variables). Its display name, planning
    override and weekly enrolment do not, and neither does any other team's entry, so
    enrolling another team (which bumps ``registry_version``) changes nothing here.

    ``portal_reviews`` (migration 008) compares exactly these three values, so a reader and
    an operator see the same lookback. An unreadable snapshot has none of them."""
    try:
        raw = json.loads(snapshot) if isinstance(snapshot, str) else None
    except ValueError:
        raw = None
    if not isinstance(raw, dict):
        return (None, None, None)
    return (
        _fragment(raw.get("v1_operators")),
        _fragment(raw.get("panels")),
        _scalar(raw.get("v2_operator")),
    )


def basis_changes(weeks: Sequence[Mapping[str, Any]]) -> list[bool]:
    """Whether each published week was measured under a different ruleset, or a different
    :func:`measurement_basis` of the team's own registry entry, than the one before it; the
    first never was. ``portal_reviews.basis_changed`` says the same."""
    bases = [measurement_basis(week["registry_entry_snapshot"]) for week in weeks]
    return [
        index > 0
        and (
            week["ruleset_version"] != weeks[index - 1]["ruleset_version"]
            or bases[index] != bases[index - 1]
        )
        for index, week in enumerate(weeks)
    ]


def read_snapshot(text: Any) -> SnapshotEntry:
    try:
        raw = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        raw = None
    if not isinstance(raw, dict):
        return SnapshotEntry()
    panels = tuple(
        SnapshotPanel(str(p.get("panel_id", "")), str(p.get("schema", "")), str(p.get("sql", "")))
        for p in raw.get("panels") or ()
        if isinstance(p, dict)
    )
    planning = raw.get("planning")
    effort = planning.get("v1_rule_effort_days") if isinstance(planning, dict) else None
    v1 = raw.get("v1_operators")
    v2 = raw.get("v2_operator")
    return SnapshotEntry(
        v1_operators=tuple(str(op) for op in v1) if isinstance(v1, list) else (),
        v2_operator=None if v2 is None else str(v2),
        panels=panels,
        v1_rule_effort_days=(
            round(float(effort), 2)
            if isinstance(effort, int | float) and not isinstance(effort, bool)
            else None
        ),
    )


def summary_inputs(
    run: Mapping[str, Any],
    daily: Sequence[Mapping[str, Any]],
    rule_counts: Sequence[Mapping[str, Any]],
    alerts: tuple[AlertRow, ...],
    *,
    published: bool,
    history: tuple[WeekRules, ...],
) -> SummaryInputs:
    readiness = run.get("phase2_readiness_pct")
    return SummaryInputs(
        surface="admin",
        team_id=str(run["team_id"]),
        display_name=str(run["team_display_name"]),
        window_start=run["window_start"],
        window_end=run["window_end"],
        phase=str(run["phase_derived"]),
        phase2_readiness_pct=None if readiness is None else float(readiness),
        schemas={schema: schema_totals(schema, daily, alerts) for schema in SCHEMAS},
        rules=rule_totals(rule_counts, alerts),
        alerts=alerts,
        published=published,
        history=history if published else (),
        v1_rule_effort_days=read_snapshot(run.get("registry_entry_snapshot")).v1_rule_effort_days,
        daily=daily_points(daily),
    )


# ------------------------------------------------------------------ queries


def worklist_where(run_id: str, filters: WorklistFilter) -> tuple[str, dict[str, Any]]:
    clauses = ["run_id = :run_id"]
    params: dict[str, Any] = {"run_id": run_id}
    if filters.state != "all":
        clauses.append("quality_state = :state")
        params["state"] = filters.state
    if filters.schema in SCHEMAS:
        clauses.append("alert_schema = :schema")
        params["schema"] = filters.schema
    if filters.rule:
        # Delimited on both sides so R1 never matches R10.
        clauses.append(
            "(',' + core_rule_ids + ',' + readiness_rule_ids + ',') LIKE '%,' + :rule + ',%'"
        )
        params["rule"] = filters.rule
    return " AND ".join(clauses), params


def last_page(total: int) -> int:
    return max(1, -(-total // PAGE_SIZE))


def _worklist(
    db: Database, run_id: str, filters: WorklistFilter
) -> tuple[tuple[AlertRow, ...], int, WorklistFilter]:
    """One page of the work list, its total, and the filters with the page clamped to the
    last one, so a page past the end shows the last page rather than nothing."""
    where, params = worklist_where(run_id, filters)
    row = db.query_one(f"SELECT COUNT(*) AS n FROM alert_findings WHERE {where}", params)
    total = int(row["n"]) if row else 0
    filters = replace(filters, page=min(max(filters.page, 1), last_page(total)))
    rows = db.query(
        f"SELECT {_ALERT_COLUMNS} FROM alert_findings WHERE {where} "
        f"ORDER BY {SORTS.get(filters.sort, SORTS['events'])} "
        "OFFSET :offset ROWS FETCH NEXT :size ROWS ONLY",
        {**params, "offset": (filters.page - 1) * PAGE_SIZE, "size": PAGE_SIZE},
    )
    return tuple(alert_row(r) for r in rows), total, filters


def _publication(db: Database, run_id: str) -> Publication:
    row = db.query_one(
        "SELECT TOP 1 published_at, published_by, review_note, withdrawn_at, withdrawn_by, "
        "withdrawn_reason FROM review_publications WHERE run_id = :run_id "
        "ORDER BY CASE WHEN withdrawn_at IS NULL THEN 0 ELSE 1 END, publication_id DESC",
        {"run_id": run_id},
    )
    if row is None:
        return Publication("never")
    if row["withdrawn_at"] is None:
        return Publication(
            "published", row["published_by"], row["published_at"], row["review_note"]
        )
    return Publication(
        "withdrawn", row["withdrawn_by"], row["withdrawn_at"], row["withdrawn_reason"]
    )


def _published_history(db: Database, team_id: str, until: datetime) -> tuple[WeekRules, ...]:
    """The team's currently published weeks up to ``until``, oldest first, with their v1
    rules. ``basis_changed`` is computed over every published week, like the portal view."""
    weeks = db.query(
        "SELECT p.run_id, p.window_end, r.ruleset_version, r.registry_entry_snapshot "
        "FROM review_publications AS p JOIN runs AS r ON r.run_id = p.run_id "
        "WHERE p.team_id = :team_id AND p.withdrawn_at IS NULL AND r.status = 'completed' "
        "ORDER BY p.window_end ASC, p.publication_id ASC",
        {"team_id": team_id},
    )
    pairs = db.query(
        "SELECT DISTINCT f.run_id, f.application, f.alert_rule_url "
        "FROM alert_findings AS f JOIN review_publications AS p ON p.run_id = f.run_id "
        "WHERE p.team_id = :team_id AND p.withdrawn_at IS NULL AND f.alert_schema = 'v1' "
        "AND p.window_end <= :until",
        {"team_id": team_id, "until": until},
    )
    rules: dict[str, set[str]] = defaultdict(set)
    for pair in pairs:
        rules[str(pair["run_id"])].add(
            v1_rule_key_of(str(pair["application"]), pair["alert_rule_url"])
        )
    return tuple(
        WeekRules(
            week_end=week["window_end"],
            v1_rules=frozenset(rules[str(week["run_id"])]),
            basis_changed=changed,
        )
        for week, changed in zip(weeks, basis_changes(weeks), strict=True)
        if week["window_end"] <= until
    )


def _completed_runs(db: Database, team_id: str) -> list[dict[str, Any]]:
    """The team's completed runs, newest first (design 7.6 ordering)."""
    return db.query(
        "SELECT TOP (60) * FROM runs WHERE team_id = :team_id AND status = 'completed' "
        "ORDER BY run_at DESC, completed_at DESC, run_id DESC",
        {"team_id": team_id},
    )


def load_admin_summary(
    db: Database, team_id: str, run_id: str | None, filters: WorklistFilter
) -> AdminSummary:
    """Everything the page shows, for the named run or the team's latest completed run."""
    runs = _completed_runs(db, team_id)
    if run_id is None:
        if not runs:
            raise SummaryNotFound("No completed run for this team.")
        run: Mapping[str, Any] = runs[0]
    else:
        found = db.query_one(
            "SELECT * FROM runs WHERE run_id = :run_id AND team_id = :team_id "
            "AND status = 'completed'",
            {"run_id": run_id, "team_id": team_id},
        )
        if found is None:
            raise SummaryNotFound("No such completed run for this team.")
        run = found
    selected = str(run["run_id"])

    daily = get_daily_metrics(db, selected)
    alerts = tuple(
        alert_row(row)
        for row in db.query(
            f"SELECT {_ALERT_COLUMNS} FROM alert_findings WHERE run_id = :run_id "
            "ORDER BY alert_schema, application, key_field",
            {"run_id": selected},
        )
    )
    publication = _publication(db, selected)
    published = publication.state == "published"
    history = _published_history(db, team_id, run["window_end"]) if published else ()
    worklist, total, filters = _worklist(db, selected, filters)
    schedule = db.query_one(
        "SELECT TOP 1 invoked_at, window_end, outcome, detail FROM weekly_review_log "
        "WHERE team_id = :team_id ORDER BY log_id DESC",
        {"team_id": team_id},
    )
    return AdminSummary(
        run=run,
        inputs=summary_inputs(
            run,
            daily,
            get_rule_counts(db, selected),
            alerts,
            published=published,
            history=history,
        ),
        entry=read_snapshot(run.get("registry_entry_snapshot")),
        daily=daily,
        stored_panels=get_run_panels(db, selected),
        runs=runs,
        publication=publication,
        schedule=schedule,
        worklist=worklist,
        total=total,
        filters=filters,
    )


# ------------------------------------------------------------------ admin-only widgets


def summary_url(team_id: str, run_id: str, **query: Any) -> str:
    params = {"run_id": run_id}
    params.update({key: str(value) for key, value in query.items() if value not in (None, "")})
    return f"/teams/{quote(team_id, safe='')}/summary?{urlencode(params)}"


def shared_sections(summary: AdminSummary) -> str:
    """The widgets both surfaces share, with each rule linking to this page's work list
    filtered by that rule (``None`` clears the filter)."""
    team_id, run_id = summary.inputs.team_id, str(summary.run["run_id"])

    def rule_link(rule_id: str | None) -> str:
        return h(summary_url(team_id, run_id, rule=rule_id) + "#worklist")

    return render_summary_sections(summarize(summary.inputs), rule_link=rule_link)


def _is_complete_day(day: Mapping[str, Any]) -> bool:
    return float(day["covered_hours"]) >= 24


def day_by_day(daily: Sequence[Mapping[str, Any]], href: str) -> str:
    """Distinct alerts on each complete UTC day, one chart per schema.

    The window's first and last buckets are partial days, so they are left out rather than
    drawn as a dip nobody caused.
    """
    cards = []
    for schema in SCHEMAS:
        days = sorted(
            (d for d in daily if d["alert_schema"] == schema and _is_complete_day(d)),
            key=lambda d: d["bucket_start"],
        )
        heading = f'<h3><span class="chip {schema}">{schema}</span> distinct alerts per day</h3>'
        if not days:
            cards.append(
                f'<article class="card hist {schema}">{heading}'
                '<p class="sub">No complete UTC day of this schema in the window.</p></article>'
            )
            continue
        points = [
            ChartPoint(
                label=f"{day['bucket_start'].day} {day['bucket_start']:%b}",
                value=int(day["distinct_alerts"]),
                window_start=day["bucket_start"],
                window_end=day["bucket_end"],
                href=f"{href}#days",
                title=(
                    f"{format_date(day['bucket_start'])}: {int(day['distinct_alerts']):,} "
                    f"distinct alerts, {int(day['alerts']):,} rows, "
                    f"{int(day['flagged_by_rule']):,} rows with a rule finding"
                ),
            )
            for day in days
        ]
        cards.append(
            f'<article class="card hist {schema}">{heading}'
            f"{line_chart(points, series=schema, label=f'{schema} distinct alerts per day')}"
            "</article>"
        )
    return f'<div class="two">{"".join(cards)}</div>'


def _close_paren(tokens: Sequence[Token], index: int) -> int:
    """Index of the ``)`` matching the ``(`` at ``index``, or the last token."""
    depth = 0
    for position in range(index, len(tokens)):
        token = tokens[position]
        if token.type == "punct" and token.value == "(":
            depth += 1
        elif token.type == "punct" and token.value == ")":
            depth -= 1
            if depth == 0:
                return position
    return len(tokens) - 1


def _operand_end(tokens: Sequence[Token], index: int) -> int:
    after = tokens[index + 1] if index + 1 < len(tokens) else None
    if after is not None and after.type == "punct" and after.value == "(":
        return _close_paren(tokens, index + 1)
    return index


def _negation_spans(tokens: Sequence[Token]) -> list[tuple[int, int]]:
    """Source spans of every negation on an instance-level field after the top-level WHERE:
    ``field != x``, ``field <> x``, ``field NOT IN (...)``, ``field NOT LIKE x``."""
    depth, start = 0, None
    for index, token in enumerate(tokens):
        if token.type == "punct" and token.value in "()":
            depth += 1 if token.value == "(" else -1
        elif token.type == "keyword" and token.value == "WHERE" and depth == 0:
            start = index + 1
            break
    if start is None:
        return []
    spans: list[tuple[int, int]] = []
    index = start
    while index < len(tokens) - 1:
        token, following = tokens[index], tokens[index + 1]
        end = None
        if token.type == "identifier" and classify_field(token.value.split(".")[-1]) == "instance":
            if following.type == "operator" and following.value in ("!=", "<>"):
                end = _operand_end(tokens, index + 2)
            elif following.type == "keyword" and following.value == "NOT":
                keyword = tokens[index + 2]
                if keyword.type == "keyword" and keyword.value == "IN":
                    end = _close_paren(tokens, index + 3)
                elif keyword.type == "keyword" and keyword.value == "LIKE":
                    end = _operand_end(tokens, index + 3)
        if end is None:
            index += 1
            continue
        last = tokens[end]
        spans.append((token.start, last.start + len(last.raw)))
        index = end + 1
    return spans


def _is_suppression_leaf(leaf: Leaf) -> bool:
    if leaf.field is None or leaf.field.kind != "field" or leaf.field.name is None:
        return False
    if classify_field(leaf.field.name) != "instance":
        return False
    return (leaf.type == "comparison" and leaf.operator in ("!=", "<>")) or (
        leaf.type in ("in", "like") and leaf.negated
    )


def highlight_panel_sql(sql: str) -> tuple[str, bool]:
    """The panel's SQL, escaped, with each suppression clause in a ``<mark>``.

    A clause nested inside ``OR`` or ``NOT`` is marked ``unmeasured``: the run could not lift
    it out safely and did not apply it (design 5.2). SQL the parser cannot read is shown
    unmarked. Returns the HTML and whether the clauses could be marked.
    """
    try:
        where = parse_panel_sql(sql)
        tokens = tokenize(sql)
    except SqlParseError:
        return h(sql), False
    nested = [
        inside
        for leaf, inside in collect_leaves(where)
        if isinstance(leaf, Leaf) and _is_suppression_leaf(leaf)
    ]
    spans = _negation_spans(tokens)
    if len(spans) != len(nested):
        # The two readings disagree; show the text rather than guess which clause is which.
        return h(sql), False
    parts, position = [], 0
    for (start, end), inside in zip(spans, nested, strict=True):
        css = "sup unmeasured" if inside else "sup"
        title = (
            "Inside OR or NOT: counted as unmeasured, not applied"
            if inside
            else "Suppression clause: the team hides these alerts"
        )
        parts.append(h(sql[position:start]))
        parts.append(f'<mark class="{css}" title="{title}">{h(sql[start:end])}</mark>')
        position = end
    parts.append(h(sql[position:]))
    return "".join(parts), True


def _panels(summary: AdminSummary) -> str:
    stored = {str(p["panel_id"]): p for p in summary.stored_panels}
    if not summary.entry.panels:
        return '<p class="sub">No dashboard supplied: no panel SQL is on record for this run.</p>'
    items = []
    for panel in summary.entry.panels:
        html, marked = highlight_panel_sql(panel.sql)
        measured = stored.get(panel.panel_id)
        facts = (
            f"{int(measured['suppression_leaves'])} suppression clause(s) applied · "
            f"{int(measured['unmeasured_leaves'])} unmeasured"
            if measured
            else "not measured by this run"
        )
        if not marked:
            facts += " · clauses not marked: the SQL could not be read clause by clause"
        items.append(
            '<li class="panel">'
            f'<div class="sub"><span class="chip {h(panel.schema)}">{h(panel.schema)}</span> '
            f'<span class="mono">{h(panel.panel_id)}</span> · {h(facts)}</div>'
            f'<pre class="sql">{html}</pre></li>'
        )
    return f'<ul class="panels">{"".join(items)}</ul>'


# ------------------------------------------------------------------ the page


def _when(value: Any) -> str:
    return format_instant(value) if isinstance(value, datetime) else "—"


def _publication_html(publication: Publication) -> str:
    if publication.state == "published":
        return (
            f'<span class="state published">published</span> {h(_when(publication.at))} '
            f"by {h(publication.by)}"
            + (f'<div class="sub">{h(publication.note)}</div>' if publication.note else "")
        )
    if publication.state == "withdrawn":
        return (
            f'<span class="state held">withdrawn</span> {h(_when(publication.at))} '
            f"by {h(publication.by)}"
            + (f'<div class="sub">{h(publication.note)}</div>' if publication.note else "")
        )
    return '<span class="state none">never published</span>'


def _schedule_html(schedule: Mapping[str, Any] | None) -> str:
    if schedule is None:
        return '<span class="state none">never scheduled</span>'
    outcome = str(schedule["outcome"])
    css = "published" if outcome == "published" else "held"
    detail = f'<div class="sub">{h(schedule["detail"])}</div>' if schedule.get("detail") else ""
    return (
        f'<span class="state {css}">{h(outcome)}</span> {h(_when(schedule["invoked_at"]))}{detail}'
    )


def _picker(summary: AdminSummary) -> str:
    team_id = summary.inputs.team_id
    selected = str(summary.run["run_id"])
    runs = list(summary.runs)
    if all(str(run["run_id"]) != selected for run in runs):
        # Older than the runs listed: offer it anyway, so the picker shows what is open.
        runs.append(summary.run)
    options = "".join(
        f'<option value="{h(run["run_id"])}"{" selected" if str(run["run_id"]) == selected else ""}>'
        f"Week of {h(format_week(run['window_start'], run['window_end']))} · "
        f"{h(str(run['run_id'])[:12])}</option>"
        for run in runs
    )
    return (
        f'<form class="picker" method="get" action="/teams/{h(quote(team_id, safe=""))}/summary">'
        f'<label class="sub" for="run_id">Run</label>'
        f'<select id="run_id" name="run_id">{options}</select>'
        '<button class="button" type="submit">Open</button></form>'
    )


def _strip(run: Mapping[str, Any]) -> str:
    facts = (
        ("Window", f"{_when(run['window_start'])} {EN_DASH} {_when(run['window_end'])}"),
        ("Model", run.get("model_version") or "off"),
        ("Ruleset", run.get("ruleset_version")),
        ("Prompt", run.get("prompt_version")),
        ("Registry", run.get("registry_version")),
        ("Completed", _when(run.get("completed_at"))),
    )
    cells = "".join(f'<span><span class="k">{h(k)}</span>{h(v)}</span>' for k, v in facts)
    return (
        f'<section class="meta runstrip">{cells}'
        f'<span><span class="k">Run</span><span class="mono">{h(run["run_id"])}</span></span>'
        "</section>"
    )


def _select(name: str, label: str, choices: Sequence[tuple[str, str]], current: str) -> str:
    options = "".join(
        f'<option value="{h(value)}"{" selected" if value == current else ""}>{h(text)}</option>'
        for value, text in choices
    )
    return f'<label>{h(label)} <select name="{name}">{options}</select></label>'


def _worklist_html(summary: AdminSummary) -> str:
    inputs, filters = summary.inputs, summary.filters
    run_id = str(summary.run["run_id"])
    team_id = inputs.team_id
    findings = f"/runs/{quote(run_id, safe='')}/findings"
    # The active rule is always offered, even when this run has no row for it, so the
    # select never shows "Any rule" while a rule filter is applied.
    rules = sorted(
        {rule.rule_id for rule in inputs.rules} | ({filters.rule} if filters.rule else set()),
        key=_rule_order,
    )
    form = (
        f'<form class="inline filters" method="get" action="/teams/{h(quote(team_id, safe=""))}/summary">'
        f'<input type="hidden" name="run_id" value="{h(run_id)}">'
        + _select(
            "state",
            "State",
            [("all", "Any state"), *((s, QUALITY_STATE_LABELS.get(s, s)) for s in STATES[1:])],
            filters.state,
        )
        + _select(
            "schema", "Schema", [("all", "v1 + v2"), ("v1", "v1"), ("v2", "v2")], filters.schema
        )
        + _select("rule", "Rule", [("", "Any rule"), *((r, r) for r in rules)], filters.rule)
        + _select(
            "sort",
            "Sort",
            [("events", "Most events"), ("application", "Application")],
            filters.sort,
        )
        + '<button class="button" type="submit">Filter</button>'
        f'<a href="{h(summary_url(team_id, run_id))}#worklist">Clear</a></form>'
    )
    rows = "".join(
        "<tr>"
        f"<td>“{h(alert.message or '(no message)')}”"
        f'<div class="sub">{h(alert.application)} &rsaquo; {h(alert.component or "—")} · '
        f'<span class="mono">{h(alert.key_field)}</span></div></td>'
        f'<td><span class="chip {h(alert.schema)}">{h(alert.schema)}</span></td>'
        f"<td>{h(QUALITY_STATE_LABELS.get(alert.quality_state, alert.quality_state))}</td>"
        f"<td>{h(', '.join((*alert.core_rule_ids, *alert.readiness_rule_ids)) or '—')}</td>"
        f'<td class="num">{alert.row_count:,}</td>'
        f'<td><a href="{h(findings)}">Findings</a></td>'
        "</tr>"
        for alert in summary.worklist
    )
    pages_count = last_page(summary.total)
    page = min(max(filters.page, 1), pages_count)
    first = (page - 1) * PAGE_SIZE + 1 if summary.total else 0
    last = min(page * PAGE_SIZE, summary.total)

    def page_link(target: int, text: str, enabled: bool) -> str:
        if not enabled:
            return f'<span class="button" aria-disabled="true">{text}</span>'
        href = summary_url(
            team_id,
            run_id,
            state=None if filters.state == "all" else filters.state,
            schema=None if filters.schema == "all" else filters.schema,
            rule=filters.rule,
            sort=None if filters.sort == "events" else filters.sort,
            page=target,
        )
        return f'<a class="button" href="{h(href)}#worklist">{text}</a>'

    return (
        '<section class="card" id="worklist"><div class="section-h tools"><h2>Work list</h2>'
        "<p>One row per distinct alert in this run. Decisions are recorded on the findings "
        "page.</p></div>"
        f'<div class="tools">{form}</div>'
        '<div class="table-wrap"><table class="runs"><thead><tr><th>Alert</th><th>Schema</th>'
        "<th>State</th><th>Rules</th><th>Rows</th><th></th></tr></thead>"
        f"<tbody>{rows or '<tr><td colspan=6>Nothing matches this filter.</td></tr>'}</tbody>"
        "</table></div>"
        f'<div class="pager"><span>Showing {first}&ndash;{last} of {summary.total:,}</span>'
        f'<span class="links">{page_link(page - 1, "Previous", page > 1)}'
        f"{page_link(page + 1, 'Next', page < pages_count)}</span></div>"
        "</section>"
    )


def summary_page(user: str, summary: AdminSummary, *, shared: str) -> str:
    """The whole page. ``shared`` is the shared summary widgets' HTML."""
    inputs, run, entry = summary.inputs, summary.run, summary.entry
    team_id, run_id = inputs.team_id, str(run["run_id"])
    readiness = (
        "—" if inputs.phase2_readiness_pct is None else f"{round(inputs.phase2_readiness_pct)}%"
    )
    operators = ", ".join(entry.v1_operators) or "none"
    body = (
        f'<nav class="crumbs"><a href="/">All teams</a><span>/</span>'
        f'<a href="/teams/{h(quote(team_id, safe=""))}">{h(inputs.display_name)}</a>'
        "<span>/</span><span>Summary</span></nav>"
        '<section class="head">'
        f'<div><div class="eyebrow">Team summary</div><h1>{h(inputs.display_name)}</h1></div>'
        f"{_picker(summary)}</section>"
        '<section class="meta">'
        f'<span><span class="k">Phase</span>{h(inputs.phase)} · readiness {h(readiness)}</span>'
        f'<span><span class="k">v1 operators</span>{h(operators)}</span>'
        f'<span><span class="k">v2 operator</span>{h(entry.v2_operator or "none")}</span>'
        f'<span><span class="k">Panels</span>{len(entry.panels)}</span>'
        f'<span><span class="k">Publication</span>{_publication_html(summary.publication)}</span>'
        f'<span><span class="k">Last schedule outcome</span>{_schedule_html(summary.schedule)}</span>'
        "</section>"
        f"{_strip(run)}"
        f'<p class="links"><a href="/runs/{h(quote(run_id, safe=""))}/scorecard">Scorecard</a> · '
        f'<a href="/runs/{h(quote(run_id, safe=""))}/findings">Findings and decisions</a></p>'
        f"{shared}"
        '<section id="days"><div class="section-h"><h2>Day by day</h2><p>Complete UTC days '
        "only; the window's partial first and last days are left out.</p></div>"
        f"{day_by_day(summary.daily, summary_url(team_id, run_id))}</section>"
        '<section class="card block" id="panels"><h2>Panel SQL</h2>'
        "<p class=\"sub\">Each panel's SQL as the run froze it. Marked clauses are the team's "
        "own suppression: applied, or unmeasured when nested inside OR or NOT.</p>"
        f"{_panels(summary)}</section>"
        f"{_worklist_html(summary)}"
    )
    return pages.layout(f"Summary · {inputs.display_name}", user, body)
