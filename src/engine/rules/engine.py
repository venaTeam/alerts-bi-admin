"""Rule execution grain and aggregation (design section 4; flow step 4).

Core rules are evaluated on EVERY raw row, then aggregated to alert identity:

- ``count`` per rule is the number of rows that actually match.
- ``distinct_count`` per rule is the number of identities with at least one matching row.
- Findings stay attached only to the rows that matched. They are never projected onto
  other rows sharing an identity, because that would move a finding onto a date where
  nothing was wrong.

LLM eligibility is decided at IDENTITY level after that row-level pass: any core finding
anywhere in the window withholds the whole identity. That deliberately accepts losing a
second, advisory judgment for the alert - a deterministic finding already gives the team a
concrete fix, whereas evaluating only the representative could miss a row-specific R5
suppression or R7 timestamp failure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from alerts_bi_shared.catalogs import CORE_RULE_IDS, V2_READINESS_RULE_IDS

from ..domain.normalize import AlertRecord, select_representative
from .core import Finding, evaluate_core_rules
from .firing import firing_facts
from .readiness import evaluate_readiness_rules

__all__ = [
    "EvaluatedIdentity",
    "EvaluatedRow",
    "Evaluation",
    "RuleBucketCount",
    "attach_row_findings",
    "compare_rule_ids",
    "compute_daily_flagged",
    "compute_daily_rule_counts",
    "count_phase2_gap_identities",
    "evaluate_rows",
]


def compare_rule_ids(rule_id: str) -> tuple[str, int]:
    """Sort key ordering R1, R2, ... R10 numerically rather than lexically.

    Without this R10 sorts between R1 and R2 in reports.
    """
    prefix = rule_id[0]
    try:
        return (prefix, int(rule_id[1:]))
    except ValueError:
        return (prefix, 0)


@dataclass(slots=True)
class EvaluatedRow:
    row: AlertRecord
    core_findings: list[Finding] = field(default_factory=list)
    readiness_findings: list[Finding] = field(default_factory=list)


@dataclass(slots=True)
class EvaluatedIdentity:
    identity: str
    schema: str
    application: str
    key_field: str
    representative: AlertRecord
    rows: list[EvaluatedRow]
    core_rule_ids: list[str]
    """Distinct core rule ids matched anywhere in the window."""
    readiness_rule_ids: list[str]
    """Distinct readiness rule ids on the representative."""
    has_core_finding: bool
    llm_eligible: bool
    present_dates: set[str]
    """UTC dates this identity appears on."""
    clear_count: int = 0
    """Rows that clear the alert: v1 severity ``clear``, v2 ``status = resolved`` (R6 fact)."""
    max_clear_cycles_24h: int = 0
    """Most fire -> clear cycles inside any rolling 24 hours (R6 fact)."""
    fire_pattern: str | None = None
    """``stuck``, ``spamming`` or ``flapping`` when R6 matched, else None."""
    max_episode_firing_rows: int = 0
    """Most firing rows in any one episode (R6 fact)."""
    open_since: datetime | None = None
    """First firing row of the open episode when the last row is firing (R6 fact)."""


@dataclass(slots=True)
class Evaluation:
    rows: list[EvaluatedRow]
    identities: dict[str, EvaluatedIdentity]


@dataclass(frozen=True, slots=True)
class RuleBucketCount:
    snapshot_date: str
    rule_id: str
    match_count: int
    """Matching raw rows in this bucket. Named to match the SQL column, and because a
    NamedTuple field called ``count`` would shadow ``tuple.count``."""
    distinct_count: int
    """Identities with at least one matching row in this bucket."""


def evaluate_rows(rows: Sequence[AlertRecord], window_end: datetime) -> Evaluation:
    """Evaluate every row of one schema and aggregate to identities.

    ``window_end`` is kept for callers; no rule reads it. R6 stuck is measured from the
    rows alone (``alerts_bi_admin.engine.rules.firing``), not to the window's end.
    """
    evaluated = [
        EvaluatedRow(
            row=row,
            core_findings=evaluate_core_rules(row),
            readiness_findings=evaluate_readiness_rules(row),
        )
        for row in rows
    ]

    grouped: dict[str, list[EvaluatedRow]] = {}
    for item in evaluated:
        grouped.setdefault(item.row.identity, []).append(item)

    identities: dict[str, EvaluatedIdentity] = {}
    for identity, group in grouped.items():
        representative = select_representative([item.row for item in group])

        # R6 judges the identity's whole firing pattern, so a match belongs to every row
        # of the identity and the per-bucket allocation then applies unchanged.
        facts = firing_facts(
            representative.schema, [item.row for item in group], representative.provider
        )
        if facts.pattern is not None:
            evidence = {
                "pattern": facts.pattern,
                "rows": len(group),
                "clear_count": facts.clear_count,
                "max_clear_cycles_24h": facts.max_clear_cycles_24h,
                "max_episode_firing_rows": facts.max_episode_firing_rows,
                # How long the open episode's firing rows span (last firing row minus
                # open_since), never the time to the window's end; None when none is open.
                "open_hours": (
                    None
                    if facts.open_span is None
                    else round(facts.open_span.total_seconds() / 3600, 2)
                ),
                "span_hours": round(facts.span.total_seconds() / 3600, 2),
                "events_per_24h": (
                    None if facts.events_per_24h is None else round(facts.events_per_24h, 2)
                ),
            }
            for item in group:
                item.core_findings.append(
                    Finding(rule_id="R6", set="core", evidence=dict(evidence))
                )

        core_rule_ids = {finding.rule_id for item in group for finding in item.core_findings}

        # Readiness is a property of the identity's CURRENT state, so it is read off the
        # representative rather than unioned over history: an alert enriched yesterday is
        # ready today, and a gap it used to have is not a gap now (design section 7.6).
        representative_eval = next(item for item in group if item.row is representative)
        readiness_rule_ids = [f.rule_id for f in representative_eval.readiness_findings]

        has_core_finding = bool(core_rule_ids)
        identities[identity] = EvaluatedIdentity(
            identity=identity,
            schema=representative.schema,
            application=representative.application,
            key_field=representative.key_field,
            representative=representative,
            rows=group,
            core_rule_ids=sorted(core_rule_ids, key=compare_rule_ids),
            readiness_rule_ids=sorted(readiness_rule_ids, key=compare_rule_ids),
            has_core_finding=has_core_finding,
            llm_eligible=not has_core_finding,
            present_dates={item.row.snapshot_date for item in group},
            clear_count=facts.clear_count,
            max_clear_cycles_24h=facts.max_clear_cycles_24h,
            fire_pattern=facts.pattern,
            max_episode_firing_rows=facts.max_episode_firing_rows,
            open_since=facts.open_since,
        )

    return Evaluation(rows=evaluated, identities=identities)


def attach_row_findings(evaluation: Evaluation, findings_by_row: Mapping[int, Finding]) -> None:
    """Attach externally-derived findings (R5) to evaluated rows, then recompute.

    R5 arrives late because suppression needs the team's panels and the complete owned row
    set, which a single-row rule cannot see. It is a core finding like any other, so it
    must be able to withhold an identity from the LLM.

    Keyed by ``id(row)`` because :class:`AlertRecord` is frozen but not hashable by value:
    two rows of a re-fired alert can be field-identical, and they must stay distinguishable.
    """
    if not findings_by_row:
        return

    for item in evaluation.rows:
        finding = findings_by_row.get(id(item.row))
        if finding is None:
            continue
        if finding.set == "core":
            item.core_findings.append(finding)
        else:
            item.readiness_findings.append(finding)

    for identity in evaluation.identities.values():
        core_rule_ids = {
            finding.rule_id for item in identity.rows for finding in item.core_findings
        }
        identity.core_rule_ids = sorted(core_rule_ids, key=compare_rule_ids)
        identity.has_core_finding = bool(core_rule_ids)
        identity.llm_eligible = not identity.has_core_finding


def compute_daily_rule_counts(
    evaluated_rows: Sequence[EvaluatedRow], snapshot_dates: Sequence[str]
) -> list[RuleBucketCount]:
    """Per-rule daily counts.

    A deterministic match is attributed to the UTC bucket containing the raw row that
    matched. ``distinct_count`` counts an identity once in each bucket where at least one
    of its rows matched - not once per window, and not on dates where it did not match.

    Readiness rules R8-R10 are counted here too, so the report can show the gap breakdown,
    but they are excluded from ``flagged_by_rule`` below.
    """
    counts: dict[tuple[str, str], int] = {}
    identities: dict[tuple[str, str], set[str]] = {}

    for item in evaluated_rows:
        date = item.row.snapshot_date
        for finding in (*item.core_findings, *item.readiness_findings):
            key = (date, finding.rule_id)
            counts[key] = counts.get(key, 0) + 1
            identities.setdefault(key, set()).add(item.row.identity)

    out: list[RuleBucketCount] = []
    for snapshot_date in snapshot_dates:
        for rule_id in (*CORE_RULE_IDS, *V2_READINESS_RULE_IDS):
            key = (snapshot_date, rule_id)
            if key not in counts:
                continue
            out.append(
                RuleBucketCount(
                    snapshot_date=snapshot_date,
                    rule_id=rule_id,
                    match_count=counts[key],
                    distinct_count=len(identities[key]),
                )
            )
    return out


def compute_daily_flagged(
    evaluated_rows: Sequence[EvaluatedRow], snapshot_dates: Sequence[str]
) -> dict[str, tuple[int, int]]:
    """``flagged_by_rule`` and ``flagged_by_rule_distinct`` per bucket.

    The union of rows with at least one CORE finding, and the count of identities with at
    least one core match in that bucket. Readiness gaps are excluded by construction.

    Returns ``{snapshot_date: (flagged_by_rule, flagged_by_rule_distinct)}``.
    """
    rows_per_date = dict.fromkeys(snapshot_dates, 0)
    identities_per_date: dict[str, set[str]] = {date: set() for date in snapshot_dates}

    for item in evaluated_rows:
        if not item.core_findings:
            continue
        date = item.row.snapshot_date
        if date not in rows_per_date:
            continue
        rows_per_date[date] += 1
        identities_per_date[date].add(item.row.identity)

    return {date: (rows_per_date[date], len(identities_per_date[date])) for date in snapshot_dates}


def count_phase2_gap_identities(identities: Sequence[EvaluatedIdentity]) -> int:
    """Count of v2 identities carrying at least one readiness gap on their representative."""
    return sum(1 for i in identities if i.schema == "v2" and i.readiness_rule_ids)
