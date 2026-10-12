"""The run orchestrator (flow steps 1-8).

One selected team, one exact 168-hour UTC window, in the order the flow document fixes:
validate the registry, read Elasticsearch, count, apply deterministic rules, evaluate
suppression, assess the remainder, derive the phase, and persist.

It never defaults to all teams.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from alerts_bi_operations.registry import load_registry, select_team, snapshot_team_entry
from alerts_bi_shared.db.connection import Database
from alerts_bi_shared.hashing import compact_json, sha256_of
from alerts_bi_shared.logging_setup import log
from alerts_bi_shared.phase import derive_phase
from alerts_bi_shared.timefmt import iso_instant
from alerts_bi_shared.versions import APP_VERSION, PARSER_VERSION, PROMPT_VERSION, RULESET_VERSION
from alerts_bi_shared.window import build_run_window

from ..config import AppConfig
from ..db.llm_audit import SqlLlmJournal
from ..db.repositories import PersistencePayload, find_verdicts
from ..domain.normalize import AlertRecord
from ..es.client import EsClient
from ..llm.assess import AssessmentOutcome, assess_alerts, mark_all_unassessed
from ..llm.client import LlmClient
from ..llm.openai_client import OpenAiLlmClient
from ..llm.prompt import build_prompt
from ..rules.readiness import phase2_readiness_pct
from .streaming import SchemaAccumulator, StreamIdentity, analyze_team

__all__ = ["RunSummary", "compute_run_id", "execute_run", "select_llm_client"]

SCHEMAS = ("v1", "v2")


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    team_id: str
    phase: str
    readiness: float | None
    v1_rows: int
    v2_rows: int
    v1_identities: int
    v2_identities: int
    llm_eligible: int
    llm_assessed: bool


def compute_run_id(
    *,
    team_id: str,
    run_at: datetime,
    window_start: datetime,
    registry_sha256: str,
    model_version: str | None,
) -> str:
    """Deterministic run identifier.

    Derived from the inputs that define the run rather than randomly generated, so
    re-running the same team over the same frozen ``run_at`` and versions replaces its own
    rows instead of accumulating near-duplicate runs. Two genuinely different runs - a
    different clock, registry or version - always get different ids.
    """
    return sha256_of(
        {
            "team_id": team_id,
            "run_at": _iso(run_at),
            "window_start": _iso(window_start),
            "registry_sha256": registry_sha256,
            "ruleset_version": RULESET_VERSION,
            "prompt_version": PROMPT_VERSION,
            "model_version": model_version,
            "app_version": APP_VERSION,
        }
    )


def _iso(value: datetime) -> str:
    return iso_instant(value)


def _naive(value: datetime) -> datetime:
    """SQL Server DATETIME2 columns take naive UTC values through this driver."""
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def select_llm_client(config: AppConfig, use_llm: bool) -> tuple[LlmClient | None, str | None]:
    """Choose the LLM client for a run.

    Tests and mock runs use the deterministic fake; the live adapter is only constructed
    when the model is explicitly enabled and configured.
    """
    if not use_llm:
        return None, "LLM assessment was disabled for this run"
    if not config.llm.enabled:
        return None, "LLM assessment is disabled by configuration (LLM_ENABLED)"
    return OpenAiLlmClient(config.llm), None


def execute_run(
    *,
    team_id: str,
    run_at: datetime,
    config: AppConfig,
    es_client: EsClient,
    llm_client: LlmClient | None,
    llm_disabled_reason: str | None = None,
    registry_path: str | None = None,
    db: Database | None = None,
) -> tuple[PersistencePayload, RunSummary]:
    """Execute one run and return the payload to persist.

    Model requests/results are journaled before final persistence. Completed report rows
    remain the caller's atomic step; the journal never exposes partial runs to readers.
    """
    started_at = datetime.now(UTC)

    # 1. Registry validation completes before any Elasticsearch query: a registry mistake
    # silently changes which alerts belong to a team, and that error is invisible in the
    # output.
    loaded = load_registry(registry_path) if registry_path else load_registry()
    team = select_team(loaded, team_id)

    window = build_run_window(run_at)
    model_version = llm_client.model_version if llm_client else None
    run_id = compute_run_id(
        team_id=team.team_id,
        run_at=window.run_at,
        window_start=window.window_start,
        registry_sha256=loaded.file_sha256,
        model_version=model_version,
    )

    log.info(
        "run.started",
        run_id=run_id,
        team_id=team.team_id,
        window_start=_iso(window.window_start),
        window_end=_iso(window.window_end),
        registry_version=loaded.registry_version,
    )

    # 2-5. Read pages and aggregate exact facts without retaining raw event documents.
    evaluation = analyze_team(es_client, team, window)
    suppression = {schema: evaluation[schema].suppression for schema in SCHEMAS}

    # 6. Assess the identities that carry no core finding.
    eligible = [
        identity.representative
        for schema in SCHEMAS
        for identity in evaluation[schema].identities.values()
        if identity.llm_eligible
    ]

    outcomes: dict[str, AssessmentOutcome]
    batch_attempts: list[dict[str, Any]] = []
    new_verdicts: list[dict[str, Any]] = []
    llm_assessed = False

    if llm_client is not None:
        existing = (
            find_verdicts(
                db,
                PROMPT_VERSION,
                llm_client.model_version,
                [(a.application, a.key_field) for a in eligible],
            )
            if db is not None
            else {}
        )
        assessment = assess_alerts(
            alerts=eligible,
            client=llm_client,
            system_prompt=build_prompt().system_prompt,
            run_id=run_id,
            prompt_version=PROMPT_VERSION,
            model_version=llm_client.model_version,
            now=started_at,
            max_batch_size=config.llm.max_batch_size,
            existing_verdicts=existing,
            journal=SqlLlmJournal(db, run_id) if db is not None else None,
            clock=lambda: datetime.now(UTC),
            audit_settings={
                "model_deployment": config.llm.model,
                "max_completion_tokens": config.llm.max_completion_tokens,
            },
        )
        outcomes = assessment.outcomes
        batch_attempts = assessment.batch_attempts
        new_verdicts = assessment.new_verdicts
        llm_assessed = True
        log.info(
            "run.llm_complete",
            run_id=run_id,
            eligible=len(eligible),
            batches=assessment.requested_batches,
            reused=assessment.reused_verdicts,
        )
    else:
        # "Good" is never inferred by subtracting flagged from total, so a run without the
        # model reports its eligible identities as unassessed with an explicit reason.
        outcomes = mark_all_unassessed(
            eligible, llm_disabled_reason or "LLM assessment did not run"
        )

    # 7. Phase derivation from distinct identity presence and readiness.
    v2_representatives = [i.representative for i in evaluation["v2"].identities.values()]
    readiness = phase2_readiness_pct(v2_representatives)
    phase = derive_phase(
        len(evaluation["v1"].identities), len(evaluation["v2"].identities), readiness
    )

    # 8. Build the persistence payload.
    payload = PersistencePayload(
        run={
            "run_id": run_id,
            "run_at": _naive(window.run_at),
            "team_id": team.team_id,
            "team_display_name": team.display_name,
            "window_start": _naive(window.window_start),
            "window_end": _naive(window.window_end),
            "registry_version": loaded.registry_version,
            "registry_sha256": loaded.file_sha256,
            "registry_entry_snapshot": snapshot_team_entry(team),
            "ruleset_version": RULESET_VERSION,
            "prompt_version": PROMPT_VERSION,
            "model_version": model_version,
            "llm_assessed": llm_assessed,
            "phase_derived": phase,
            "phase2_readiness_pct": readiness,
            "app_version": APP_VERSION,
            "status": "completed",
            "started_at": _naive(started_at),
            "completed_at": _naive(datetime.now(UTC)),
            "error_summary": None,
        },
        batch_attempts=batch_attempts,
        verdicts=new_verdicts,
    )

    snapshot_dates = window.snapshot_dates
    seen_parses: set[tuple[str, str]] = set()

    for schema in SCHEMAS:
        daily = evaluation[schema].daily_volume()
        flagged = evaluation[schema].daily_flagged()
        quality = _allocate_quality_by_date(evaluation[schema], outcomes, snapshot_dates)
        suppressed_by_date = evaluation[schema].daily_visibility("suppressed")
        unseen_by_date = (
            evaluation[schema].daily_visibility("unseen") if team.panels_for(schema) else None
        )

        for index, day in enumerate(daily):
            flagged_rows, flagged_distinct = flagged[day.snapshot_date]
            q = quality[day.snapshot_date]
            payload.daily_metrics.append(
                {
                    "run_id": run_id,
                    "team_id": team.team_id,
                    "alert_schema": schema,
                    "snapshot_date": date.fromisoformat(day.snapshot_date),
                    "bucket_start": _naive(day.bucket_start),
                    "bucket_end": _naive(day.bucket_end),
                    "covered_hours": day.covered_hours,
                    "alerts": day.alerts,
                    "distinct_alerts": day.distinct_alerts,
                    "alerts_per_hour": day.alerts_per_hour,
                    "node_name_numerator": day.node_name_numerator,
                    "node_name_denominator": day.node_name_denominator,
                    "node_name_ratio": day.node_name_ratio,
                    "key_inflation_numerator": day.key_inflation_numerator,
                    "key_inflation_denominator": day.key_inflation_denominator,
                    "key_inflation_ratio": day.key_inflation_ratio,
                    "flagged_by_rule": flagged_rows,
                    "flagged_by_rule_distinct": flagged_distinct,
                    "flagged_by_llm": q["flagged_by_llm"],
                    "flagged_by_llm_distinct": q["flagged_by_llm_distinct"],
                    "needs_review": q["needs_review"],
                    "assessed_good": q["assessed_good"],
                    "unassessed": q["unassessed"],
                    "phase2_gaps": q["phase2_gaps"],
                    "suppressed": suppressed_by_date[day.snapshot_date],
                    # A leaf is unmeasured regardless of date, so the run-level count is
                    # allocated to the schema's first bucket and zero elsewhere. Summing
                    # the daily column then yields the run total exactly once instead of
                    # eight times.
                    "suppression_unmeasured": (
                        suppression[schema].unmeasured_leaves if index == 0 else 0
                    ),
                    # `unseen` is NULL, never 0, for a schema with no panel. Its unmeasured
                    # count sits on the first bucket for the same reason as above.
                    "unseen": (
                        None if unseen_by_date is None else unseen_by_date[day.snapshot_date]
                    ),
                    "unseen_unmeasured": (
                        None
                        if unseen_by_date is None
                        else (suppression[schema].unseen_unmeasured if index == 0 else 0)
                    ),
                }
            )

        for count in evaluation[schema].daily_rules():
            payload.rule_counts.append(
                {
                    "run_id": run_id,
                    "team_id": team.team_id,
                    "alert_schema": schema,
                    "snapshot_date": date.fromisoformat(count.snapshot_date),
                    "rule_id": count.rule_id,
                    "ruleset_version": RULESET_VERSION,
                    "match_count": count.match_count,
                    "distinct_count": count.distinct_count,
                }
            )

        for identity in evaluation[schema].identities.values():
            payload.findings.append(
                _build_finding_row(
                    run_id,
                    identity,
                    outcomes.get(identity.identity),
                    None,
                )
            )

        for interpretation in suppression[schema].interpretations:
            suppression_leaves = sum(
                1 for leaf in interpretation.leaves if leaf.kind == "suppression"
            )
            unmeasured = sum(1 for leaf in interpretation.leaves if leaf.kind == "unmeasured")
            payload.run_panels.append(
                {
                    "run_id": run_id,
                    "panel_id": interpretation.panel_id,
                    "alert_schema": interpretation.schema,
                    "sql_text_hash": interpretation.sql_text_hash,
                    "parser_version": interpretation.parser_version,
                    "suppression_leaves": suppression_leaves,
                    "unmeasured_leaves": (
                        1 if interpretation.safety_state == "unparseable" else unmeasured
                    ),
                    "notes": compact_json(
                        {
                            "unknown_fields": interpretation.unknown_fields,
                            "unmeasured_reason": interpretation.unmeasured_reason,
                        }
                    ),
                }
            )
            parse_key = (interpretation.sql_text_hash, PARSER_VERSION)
            if parse_key not in seen_parses:
                seen_parses.add(parse_key)
                payload.panel_parses.append(
                    {
                        "sql_text_hash": interpretation.sql_text_hash,
                        "parser_version": PARSER_VERSION,
                        "parsed_result": compact_json(
                            [leaf.to_public() for leaf in interpretation.leaves]
                        ),
                        "safety_state": interpretation.safety_state,
                        "unmeasured_reason": interpretation.unmeasured_reason,
                        "created_at": _naive(started_at),
                    }
                )

    summary = RunSummary(
        run_id=run_id,
        team_id=team.team_id,
        phase=phase,
        readiness=readiness,
        v1_rows=evaluation["v1"].row_count,
        v2_rows=evaluation["v2"].row_count,
        v1_identities=len(evaluation["v1"].identities),
        v2_identities=len(evaluation["v2"].identities),
        llm_eligible=len(eligible),
        llm_assessed=llm_assessed,
    )
    return payload, summary


def _allocate_quality_by_date(
    evaluation: SchemaAccumulator,
    outcomes: dict[str, AssessmentOutcome],
    snapshot_dates: list[str],
) -> dict[str, dict[str, int]]:
    """Allocate identity-level LLM states to UTC dates.

    An LLM verdict belongs to the identity, not to a row: a high-confidence catalogue
    violation projects ``flagged_by_llm`` onto every raw row under that identity, and
    counts the identity once in ``flagged_by_llm_distinct`` for every bucket in which it
    appears. ``needs_review``, ``assessed_good`` and ``unassessed`` likewise count the
    identity once in every bucket where it appears.

    That differs deliberately from deterministic findings, which stay on the dates of the
    rows that actually matched.
    """
    accumulator = {
        date_key: {
            "flagged_by_llm": 0,
            "flagged_by_llm_distinct": 0,
            "needs_review": 0,
            "assessed_good": 0,
            "unassessed": 0,
            "phase2_gaps": 0,
        }
        for date_key in snapshot_dates
    }

    for identity in evaluation.identities.values():
        # Readiness gaps are orthogonal to the quality state and may coexist with any of
        # them.
        if identity.schema == "v2" and identity.readiness_rule_ids:
            for date_key in identity.present_dates:
                if date_key in accumulator:
                    accumulator[date_key]["phase2_gaps"] += 1

        if identity.has_core_finding:
            continue  # rule_flagged: not an LLM state
        outcome = outcomes.get(identity.identity)
        if outcome is None:
            continue

        rows_per_date = {date: day.count for date, day in identity.days.items()}

        for date_key in identity.present_dates:
            entry = accumulator.get(date_key)
            if entry is None:
                continue
            if outcome.state == "llm_flagged":
                entry["flagged_by_llm"] += rows_per_date.get(date_key, 0)
                entry["flagged_by_llm_distinct"] += 1
            elif outcome.state in ("needs_review", "assessed_good", "unassessed"):
                entry[outcome.state] += 1

    return accumulator


def _build_finding_row(
    run_id: str,
    identity: Any,
    outcome: AssessmentOutcome | None,
    unseen_row_ids: set[int] | None,
) -> dict[str, Any]:
    representative: AlertRecord = identity.representative
    if isinstance(identity, StreamIdentity):
        row_count = identity.row_count
        first_seen = identity.first_seen
        last_seen = representative.timestamp
        evidence_rows = identity.evidence()
        unseen = identity.unseen
    else:
        timestamps = [evaluated.row.timestamp for evaluated in identity.rows]

        # Evidence is summarized per rule rather than per row: a v1 alert re-firing every five
        # minutes would otherwise store thousands of near-identical evidence objects.
        evidence: dict[str, dict[str, Any]] = {}
        for evaluated in identity.rows:
            for finding in (*evaluated.core_findings, *evaluated.readiness_findings):
                existing = evidence.get(finding.rule_id)
                if existing is not None:
                    existing["matched_rows"] += 1
                else:
                    evidence[finding.rule_id] = {
                        "rule_id": finding.rule_id,
                        "matched_rows": 1,
                        "sample_evidence": finding.evidence,
                    }
        row_count = len(identity.rows)
        first_seen, last_seen = min(timestamps), max(timestamps)
        evidence_rows = list(evidence.values())
        unseen = (
            None
            if unseen_row_ids is None
            else any(id(evaluated.row) in unseen_row_ids for evaluated in identity.rows)
        )

    state = (
        "rule_flagged"
        if identity.has_core_finding
        else (outcome.state if outcome else "unassessed")
    )
    is_rule_flagged = state == "rule_flagged"

    return {
        "run_id": run_id,
        "alert_schema": identity.schema,
        "application": identity.application,
        "key_field": identity.key_field,
        "representative_at": _naive(representative.timestamp),
        "representative_hash": representative.doc_hash,
        "representative_doc": compact_json(representative.source),
        "message": representative.message,
        "severity": representative.severity,
        "component": representative.component,
        "node_name": representative.node_name,
        "environment": representative.environment,
        "provider": representative.provider,
        "alert_rule_url": representative.alert_rule_url,
        "row_count": row_count,
        "first_seen": _naive(first_seen),
        "last_seen": _naive(last_seen),
        "core_rule_ids": ",".join(identity.core_rule_ids),
        "readiness_rule_ids": ",".join(identity.readiness_rule_ids),
        "findings_evidence": compact_json(evidence_rows),
        "quality_state": state,
        "llm_principle_id": None
        if is_rule_flagged
        else (outcome.principle_id if outcome else None),
        "llm_confidence": None if is_rule_flagged else (outcome.confidence if outcome else None),
        "llm_justification": (
            None if is_rule_flagged else (outcome.justification if outcome else None)
        ),
        "unassessed_reason": (
            (outcome.unassessed_reason if outcome else None)
            or "identity was not assessed in this run"
            if state == "unassessed"
            else None
        ),
        "clear_count": identity.clear_count,
        "max_clear_cycles_24h": identity.max_clear_cycles_24h,
        "fire_pattern": identity.fire_pattern,
        "max_episode_firing_rows": identity.max_episode_firing_rows,
        "open_since": None if identity.open_since is None else _naive(identity.open_since),
        # NULL with no panel; otherwise true when any row of the identity is unseen.
        "unseen": unseen,
    }
