"""The data shapes of the team summary. Inputs come from SQL; outputs feed the pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

Surface = Literal["admin", "portal"]
"""Which page asks. The portal never shows per-day rates or internals (design 7.10)."""


@dataclass(frozen=True, slots=True)
class SchemaTotals:
    """One schema's figures for the selected run or published week."""

    schema: str
    events: int
    """Raw rows in the 168-hour window (``sum(daily_metrics.alerts)``)."""
    distinct_alerts: int
    """Distinct identities in the whole window: the work-list row count (portal headline)."""
    distinct_per_day: float | None
    """``sum(daily distinct)/7`` (design 3.3). Admin only; None on the portal."""
    rule_flagged_events: int | None
    """None when overlapping rule matches cannot reconstruct the event union for a subset."""
    rule_flagged_alerts: int
    suppressed: int
    unseen: int | None
    """Rows no panel shows. None when the schema has no supplied panel."""
    unseen_alerts: int | None
    states: dict[str, int]
    """Identity counts per quality_state: rule_flagged, llm_flagged, needs_review,
    assessed_good, unassessed. Missing keys mean zero."""
    readiness_gaps: int
    suppression_unmeasured: int | None = None
    """Admin only."""
    unseen_unmeasured: int | None = None
    """Admin only."""


@dataclass(frozen=True, slots=True)
class RuleTotal:
    schema: str
    rule_id: str
    events: int
    """Matching raw rows in the window."""
    alerts: int
    """Distinct identities in the window carrying the rule."""
    distinct_per_day: float | None = None
    """``sum(daily distinct_count)/7``. Admin only."""


@dataclass(frozen=True, slots=True)
class AlertRow:
    """One work-list row (one identity) as stored in ``alert_findings``."""

    schema: str
    application: str
    key_field: str
    message: str | None
    severity: str | None
    provider: str | None
    alert_rule_url: str | None
    component: str | None
    node_name: str | None
    row_count: int
    first_seen: datetime
    last_seen: datetime
    quality_state: str
    core_rule_ids: tuple[str, ...]
    readiness_rule_ids: tuple[str, ...]
    llm_principle_id: str | None
    llm_confidence: str | None
    clear_count: int
    max_clear_cycles_24h: int
    fire_pattern: str | None
    unseen: bool | None
    max_episode_firing_rows: int = 0
    open_since: datetime | None = None


@dataclass(frozen=True, slots=True)
class WeekRules:
    """The v1 alert rules that fired in one published week, for the time-to-v2 estimate."""

    week_end: datetime
    v1_rules: frozenset[str]
    """Rule keys from :func:`alerts_bi_shared.insights.estimate.v1_rule_key`."""
    basis_changed: bool
    """This week was measured under a different ruleset/registry than the one before it."""


@dataclass(frozen=True, slots=True)
class DailyPoint:
    """One schema's UTC day bucket of the run (``daily_metrics``), for the slides' charts."""

    alert_schema: str
    day: date
    """``snapshot_date``."""
    covered_hours: float
    """Below 24 for the partial first and last day of a week that does not start at midnight."""
    distinct_alerts: int
    rule_flagged_distinct: int
    """``flagged_by_rule_distinct``: identities with a core rule finding on that day."""


@dataclass(frozen=True, slots=True)
class SummaryInputs:
    surface: Surface
    team_id: str
    display_name: str
    window_start: datetime
    window_end: datetime
    phase: str
    phase2_readiness_pct: float | None
    schemas: dict[str, SchemaTotals]
    """Keyed ``v1`` and ``v2``; both always present."""
    rules: tuple[RuleTotal, ...]
    alerts: tuple[AlertRow, ...]
    published: bool
    """The selected run is a currently published week. The estimate needs it."""
    history: tuple[WeekRules, ...]
    """Published weeks of the team up to and including the selected one, oldest first.
    Empty when the selected run is not published. With stored facts, only the four
    relevant week boundaries/basis flags are loaded; exact rule counts and retirements
    are SQL aggregates in facts rather than materialized identity sets."""
    v1_rule_effort_days: float | None
    """The team's registry override, or None for the default."""
    daily: tuple[DailyPoint, ...] = ()
    """The run's day buckets, ordered by schema then day. Empty when not loaded."""
    applications: tuple[str, ...] | None = None
    """Portal selection; None means all. Historical subset daily buckets may be unavailable.
    Published phase and readiness percentage always describe the whole team."""
    facts: dict[str, Any] | None = None
    """Exact stored aggregates. When present, alerts contains only bounded examples."""


@dataclass(frozen=True, slots=True)
class KeyFinding:
    kind: Literal["largest", "concentration", "hidden", "unseen", "readiness", "unassessed"]
    title: str
    body: str
    fix: str | None
    rule_filter: str | None
    """Rule id the "show these alerts" link filters the work list by, if any."""


@dataclass(frozen=True, slots=True)
class AppRow:
    application: str
    schema: str
    alerts: int
    rule_flagged_alerts: int
    llm_flagged_alerts: int
    events: int
    rule_flagged_events: int
    llm_flagged_events: int
    rules: tuple[str, ...]
    """Core rule ids seen, in catalogue order."""


@dataclass(frozen=True, slots=True)
class FireRow:
    alert: AlertRow
    span_hours: float
    """``last_seen - first_seen``, in hours."""
    max_episode_firing_rows: int
    """The most firing rows in any one episode."""
    open_hours: float | None
    """Hours from ``open_since`` to the open episode's last firing row (how long its firing
    rows span); None when no episode is open."""
    events_per_24h: float | None
    """None when the span is under 6 hours. Display only."""
    pattern: str | None
    """The stored ``fire_pattern``."""


@dataclass(frozen=True, slots=True)
class Estimate:
    rules_left: int
    lookback_weeks: int
    """Earlier published weeks used, 0-3."""
    retired: int | None
    pace_per_week: float | None
    projected_week_end: date | None
    no_estimate_reason: str | None
    """Set when no date is shown: not published, too few earlier weeks, too few retired."""
    effort_days_per_rule: float
    effort_is_override: bool
    effort_days: float
    effort_weeks: float


@dataclass(frozen=True, slots=True)
class TeamSummary:
    inputs: SummaryInputs
    key_findings: tuple[KeyFinding, ...]
    by_application: tuple[AppRow, ...]
    fire: tuple[FireRow, ...]
    biggest: AlertRow | None
    estimate: Estimate
