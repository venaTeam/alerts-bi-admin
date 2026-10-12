"""Incremental weekly facts. Memory follows identities and distinct scopes, not events.

Every row still visits the approved rule functions. Predicate signatures preserve the
intersection of R5, other core findings and unseen until the whole-schema guard is known.
The latest complete document and one sample per finding survive; repeated raw rows do not.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from alerts_bi_operations.registry import Panel, TeamEntry
from alerts_bi_shared.catalogs import CORE_RULE_IDS, V2_READINESS_RULE_IDS
from alerts_bi_shared.hashing import sha256_of
from alerts_bi_shared.window import RunWindow

from ..domain.metrics import DailyVolume, ratio_or_none
from ..domain.normalize import AlertRecord
from ..es.client import EsClient
from ..es.reader import scan_schema
from ..rules.core import Finding, evaluate_core_rules
from ..rules.engine import RuleBucketCount, compare_rule_ids
from ..rules.firing_stream import FiringAccumulator
from ..rules.readiness import evaluate_readiness_rules
from ..suppression.stream import SuppressionAccumulator

# Ordering within a raw row in the existing evidence serializer: core, R6, R5, readiness.
_EVIDENCE_ORDER = {
    r: i for i, r in enumerate(("R1", "R2", "R3", "R4", "R7", "R6", "R5", "R8", "R9", "R10"))
}


@dataclass(slots=True)
class IdentityDay:
    count: int = 0
    rules: dict[str, int] = field(default_factory=dict)
    # (suppression leaf mask, any ordinary core finding, unanimously hidden) ->
    # (number of events, earliest event ordinal). No event text is retained here.
    signatures: dict[tuple[int, bool, bool], tuple[int, int]] = field(default_factory=dict)
    flagged: int = 0
    suppressed: int = 0
    unseen: int = 0


@dataclass(slots=True)
class StreamIdentity:
    representative: AlertRecord
    first_seen: datetime
    first_ordinal: int
    days: dict[str, IdentityDay] = field(default_factory=dict)
    samples: dict[str, dict[str, Any]] = field(default_factory=dict)
    sample_order: dict[str, tuple[int, int]] = field(default_factory=dict)
    core_rule_ids: list[str] = field(default_factory=list)
    readiness_rule_ids: list[str] = field(default_factory=list)
    firing: FiringAccumulator = field(default_factory=FiringAccumulator)
    clear_count: int = 0
    max_clear_cycles_24h: int = 0
    max_episode_firing_rows: int = 0
    open_since: datetime | None = None
    fire_pattern: str | None = None
    unseen: bool | None = None

    @property
    def identity(self) -> str:
        return self.representative.identity

    @property
    def schema(self) -> str:
        return self.representative.schema

    @property
    def application(self) -> str:
        return self.representative.application

    @property
    def key_field(self) -> str:
        return self.representative.key_field

    @property
    def has_core_finding(self) -> bool:
        return bool(self.core_rule_ids)

    @property
    def llm_eligible(self) -> bool:
        return not self.has_core_finding

    @property
    def present_dates(self) -> set[str]:
        return set(self.days)

    @property
    def row_count(self) -> int:
        return sum(d.count for d in self.days.values())

    def evidence(self) -> list[dict[str, Any]]:
        return [self.samples[r] for r in sorted(self.samples, key=self.sample_order.__getitem__)]

    def record(self, finding: Finding, ordinal: int, count: int = 1) -> None:
        rule = finding.rule_id
        if rule not in self.samples:
            self.samples[rule] = {
                "rule_id": rule,
                "matched_rows": count,
                "sample_evidence": finding.evidence,
            }
            self.sample_order[rule] = (ordinal, _EVIDENCE_ORDER[rule])
        else:
            self.samples[rule]["matched_rows"] += count


@dataclass(slots=True)
class ScopeDay:
    count: int = 0
    scopes: set[tuple[str, str]] = field(default_factory=set)
    nodes: set[tuple[str, str, str]] = field(default_factory=set)
    node_scopes: set[tuple[str, str]] = field(default_factory=set)


class SchemaAccumulator:
    def __init__(self, schema: str, window: RunWindow, panels: Sequence[Panel]) -> None:
        self.schema = schema
        self.window = window
        self.identities: dict[str, StreamIdentity] = {}
        self.scopes = {day: ScopeDay() for day in window.snapshot_dates}
        self.visibility = SuppressionAccumulator(panels)
        self.panels = panels
        self.row_count = 0
        self.last_at: datetime | None = None
        self.finished = False

    def add(self, row: AlertRecord) -> None:
        if self.finished:
            raise ValueError("cannot add to finished analysis")
        if row.schema != self.schema or not self.window.contains(row.timestamp):
            raise ValueError("event is outside the selected schema/window")
        if self.last_at is not None and row.timestamp < self.last_at:
            raise ValueError("streaming analysis requires chronological Elasticsearch pages")
        self.last_at = row.timestamp
        ordinal = self.row_count
        self.row_count += 1
        identity = self.identities.get(row.identity)
        if identity is None:
            identity = StreamIdentity(row, row.timestamp, ordinal)
            self.identities[row.identity] = identity
        else:
            representative = identity.representative
            if row.timestamp > representative.timestamp:
                identity.representative = row
            elif row.timestamp == representative.timestamp:
                # Full hashes matter only to select among equally recent candidates.
                if not representative.doc_hash:
                    representative = replace(
                        representative, doc_hash=sha256_of(representative.source)
                    )
                    identity.representative = representative
                if not row.doc_hash:
                    row = replace(row, doc_hash=sha256_of(row.source))
                if row.doc_hash < representative.doc_hash:
                    identity.representative = row

        identity.firing.add(row)
        day = identity.days.get(row.snapshot_date)
        if day is None:
            day = IdentityDay()
            identity.days[row.snapshot_date] = day
        day.count += 1
        core = evaluate_core_rules(row)
        for finding in (*core, *evaluate_readiness_rules(row)):
            day.rules[finding.rule_id] = day.rules.get(finding.rule_id, 0) + 1
            identity.record(finding, ordinal)
        mask, hidden = self.visibility.add(row)
        signature = (mask, bool(core), hidden)
        count, first = day.signatures.get(signature, (0, ordinal))
        day.signatures[signature] = count + 1, first

        scope_day = self.scopes[row.snapshot_date]
        scope_day.count += 1
        scope = (row.application, row.component or "")
        scope_day.scopes.add(scope)
        if row.node_name is not None and row.node_name.strip():
            scope_day.nodes.add((*scope, row.node_name))
            scope_day.node_scopes.add(scope)

    def finish(self) -> None:
        if self.finished:
            raise ValueError("analysis already finished")
        self.finished = True
        self.suppression = self.visibility.finish()
        for identity in self.identities.values():
            representative = identity.representative
            if not representative.doc_hash:
                identity.representative = replace(
                    representative, doc_hash=sha256_of(representative.source)
                )
            facts = identity.firing.finish(representative.provider)
            identity.clear_count = facts.clear_count
            identity.max_clear_cycles_24h = facts.max_clear_cycles_24h
            identity.max_episode_firing_rows = facts.max_episode_firing_rows
            identity.open_since = facts.open_since
            identity.fire_pattern = facts.pattern
            if facts.pattern:
                evidence = {
                    "pattern": facts.pattern,
                    "rows": identity.row_count,
                    "clear_count": facts.clear_count,
                    "max_clear_cycles_24h": facts.max_clear_cycles_24h,
                    "max_episode_firing_rows": facts.max_episode_firing_rows,
                    "open_hours": None
                    if facts.open_span is None
                    else round(facts.open_span.total_seconds() / 3600, 2),
                    "span_hours": round(facts.span.total_seconds() / 3600, 2),
                    "events_per_24h": None
                    if facts.events_per_24h is None
                    else round(facts.events_per_24h, 2),
                }
                identity.record(
                    Finding("R6", "core", evidence), identity.first_ordinal, identity.row_count
                )
            first_suppressed: int | None = None
            suppressed_count = 0
            for day in identity.days.values():
                if facts.pattern:
                    day.rules["R6"] = day.count
                for (mask, core, hidden), (count, first) in day.signatures.items():
                    suppressed = self.visibility.suppressed(mask)
                    if suppressed:
                        day.suppressed += count
                        first_suppressed = (
                            first if first_suppressed is None else min(first_suppressed, first)
                        )
                    if core or suppressed or facts.pattern:
                        day.flagged += count
                    if hidden and not suppressed:
                        day.unseen += count
                if day.suppressed:
                    day.rules["R5"] = day.suppressed
                    suppressed_count += day.suppressed
                day.signatures.clear()
            if first_suppressed is not None:
                identity.record(
                    Finding(
                        "R5",
                        "core",
                        {
                            "panels": [p.panel_id for p in self.panels],
                            "reason": "excluded by every supplied panel for this schema",
                        },
                    ),
                    first_suppressed,
                    suppressed_count,
                )
            identity.core_rule_ids = sorted(
                (r for r in identity.samples if r in CORE_RULE_IDS), key=compare_rule_ids
            )
            identity.readiness_rule_ids = [
                f.rule_id for f in evaluate_readiness_rules(identity.representative)
            ]
            identity.unseen = any(d.unseen for d in identity.days.values()) if self.panels else None

    def daily_volume(self) -> list[DailyVolume]:
        distinct = dict.fromkeys(self.scopes, 0)
        for identity in self.identities.values():
            for date_key in identity.days:
                distinct[date_key] += 1
        result = []
        for bucket in self.window.buckets:
            day = self.scopes[bucket.snapshot_date]
            keys = distinct[bucket.snapshot_date]
            result.append(
                DailyVolume(
                    bucket.snapshot_date,
                    bucket.bucket_start,
                    bucket.bucket_end,
                    bucket.covered_hours,
                    day.count,
                    keys,
                    day.count / bucket.covered_hours,
                    len(day.nodes),
                    len(day.node_scopes),
                    ratio_or_none(len(day.nodes), len(day.node_scopes)),
                    keys,
                    len(day.scopes),
                    ratio_or_none(keys, len(day.scopes)),
                )
            )
        return result

    def daily_flagged(self) -> dict[str, tuple[int, int]]:
        result = dict.fromkeys(self.scopes, (0, 0))
        for identity in self.identities.values():
            for date, day in identity.days.items():
                rows, distinct = result[date]
                result[date] = rows + day.flagged, distinct + bool(day.flagged)
        return result

    def daily_visibility(self, attribute: str) -> dict[str, int]:
        result = dict.fromkeys(self.scopes, 0)
        for identity in self.identities.values():
            for date, day in identity.days.items():
                result[date] += getattr(day, attribute)
        return result

    def daily_rules(self) -> list[RuleBucketCount]:
        totals: dict[tuple[str, str], tuple[int, int]] = {}
        for identity in self.identities.values():
            for date, day in identity.days.items():
                for rule, count in day.rules.items():
                    rows, distinct = totals.get((date, rule), (0, 0))
                    totals[date, rule] = rows + count, distinct + 1
        return [
            RuleBucketCount(date, rule, *totals[date, rule])
            for date in self.scopes
            for rule in (*CORE_RULE_IDS, *V2_READINESS_RULE_IDS)
            if (date, rule) in totals
        ]


def analyze_team(
    client: EsClient, team: TeamEntry, window: RunWindow
) -> dict[str, SchemaAccumulator]:
    result = {}
    for schema, operators in (
        ("v1", team.v1_operators),
        ("v2", [] if team.v2_operator is None else [team.v2_operator]),
    ):
        analysis = SchemaAccumulator(schema, window, team.panels_for(schema))
        scan_schema(client, schema, operators, window, analysis.add, hash_documents=False)
        analysis.finish()
        result[schema] = analysis
    return result
