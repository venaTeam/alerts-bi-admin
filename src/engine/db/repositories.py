"""Repositories over the run store.

Two write scopes exist and they are deliberately different:

- Run-scoped rows (metrics, rule counts, findings, batch attempts, panels) are replaced
  wholesale when a run id is re-persisted, so a restarted run cannot leave a mixture of two
  attempts behind.
- Durable rows (``llm_verdicts``, ``panel_parses``) are inserted only when absent. A stored
  verdict is never recomputed except on a version bump, and a frozen panel interpretation
  must stay stable between runs on identical input.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from alerts_bi_operations.db.reports import (
    get_batch_attempts,
    get_daily_metrics,
    get_findings,
    get_latest_run,
    get_rule_counts,
    get_run,
    get_run_panels,
)
from alerts_bi_shared.db.connection import Database

__all__ = [
    "PersistencePayload",
    "RunIsPublished",
    "find_panel_parse",
    "find_verdicts",
    "get_batch_attempts",
    "get_daily_metrics",
    "get_findings",
    "get_latest_run",
    "get_rule_counts",
    "get_run",
    "get_run_panels",
    "persist_run",
    "verdict_key",
]


class RunIsPublished(ValueError):
    """A run readers can currently see may not be re-persisted underneath them."""


def verdict_key(application: str, key_field: str) -> str:
    """Map key for a durable verdict lookup.

    Length-prefixed rather than joined with a separator: application and key_field are
    free text from the sending team, so any printable separator could appear inside a value
    and make two different alerts collide on one key.
    """
    return f"{len(application)}:{application}|{len(key_field)}:{key_field}"


_DAILY_METRIC_COLUMNS = (
    "run_id",
    "team_id",
    "alert_schema",
    "snapshot_date",
    "bucket_start",
    "bucket_end",
    "covered_hours",
    "alerts",
    "distinct_alerts",
    "alerts_per_hour",
    "node_name_numerator",
    "node_name_denominator",
    "node_name_ratio",
    "key_inflation_numerator",
    "key_inflation_denominator",
    "key_inflation_ratio",
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
    "unseen",
    "unseen_unmeasured",
)

_RULE_COUNT_COLUMNS = (
    "run_id",
    "team_id",
    "alert_schema",
    "snapshot_date",
    "rule_id",
    "ruleset_version",
    "match_count",
    "distinct_count",
)

_FINDING_COLUMNS = (
    "run_id",
    "alert_schema",
    "application",
    "key_field",
    "representative_at",
    "representative_hash",
    "representative_doc",
    "message",
    "severity",
    "component",
    "node_name",
    "environment",
    "provider",
    "alert_rule_url",
    "row_count",
    "first_seen",
    "last_seen",
    "core_rule_ids",
    "readiness_rule_ids",
    "findings_evidence",
    "quality_state",
    "llm_principle_id",
    "llm_confidence",
    "llm_justification",
    "unassessed_reason",
    "clear_count",
    "max_clear_cycles_24h",
    "fire_pattern",
    "unseen",
    "max_episode_firing_rows",
    "open_since",
)

_BATCH_ATTEMPT_COLUMNS = (
    "run_id",
    "batch_id",
    "attempt_number",
    "group_type",
    "group_value",
    "partition_index",
    "partition_count",
    "alert_count",
    "alert_ids",
    "request_hash",
    "request_payload",
    "status",
    "failure_reason",
    "duration_ms",
    "created_at",
)

_RUN_PANEL_COLUMNS = (
    "run_id",
    "panel_id",
    "alert_schema",
    "sql_text_hash",
    "parser_version",
    "suppression_leaves",
    "unmeasured_leaves",
    "notes",
)

_RUN_COLUMNS = (
    "run_id",
    "run_at",
    "team_id",
    "team_display_name",
    "window_start",
    "window_end",
    "registry_version",
    "registry_sha256",
    "registry_entry_snapshot",
    "ruleset_version",
    "prompt_version",
    "model_version",
    "llm_assessed",
    "phase_derived",
    "phase2_readiness_pct",
    "app_version",
    "status",
    "started_at",
    "completed_at",
    "error_summary",
)

_VERDICT_COLUMNS = (
    "application",
    "key_field",
    "prompt_version",
    "model_version",
    "alert_schema",
    "assessment",
    "principle_id",
    "confidence",
    "justification",
    "representative_doc",
    "doc_hash",
    "classified_at",
    "ruleset_version",
    "first_run_id",
)

_PANEL_PARSE_COLUMNS = (
    "sql_text_hash",
    "parser_version",
    "parsed_result",
    "safety_state",
    "unmeasured_reason",
    "created_at",
)


def _insert_statement(table: str, columns: Sequence[str]) -> str:
    names = ", ".join(columns)
    placeholders = ", ".join(f":{name}" for name in columns)
    return f"INSERT INTO {table} ({names}) VALUES ({placeholders})"


#: Columns whose absence means a counted zero rather than NULL: the R6 facts are counts, and
#: a row built before they existed simply observed no clears.
_ZERO_DEFAULTS: Final = frozenset(
    {"clear_count", "max_clear_cycles_24h", "max_episode_firing_rows"}
)


def _project(rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> list[dict[str, Any]]:
    """Keep exactly the declared columns, defaulting anything absent to NULL (or 0 for a
    count in ``_ZERO_DEFAULTS``)."""
    return [
        {name: row.get(name, 0 if name in _ZERO_DEFAULTS else None) for name in columns}
        for row in rows
    ]


@dataclass(slots=True)
class PersistencePayload:
    run: dict[str, Any]
    daily_metrics: list[dict[str, Any]] = field(default_factory=list)
    rule_counts: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    batch_attempts: list[dict[str, Any]] = field(default_factory=list)
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    panel_parses: list[dict[str, Any]] = field(default_factory=list)
    run_panels: list[dict[str, Any]] = field(default_factory=list)


def persist_run(db: Database, payload: PersistencePayload) -> None:
    """Persist a complete run atomically.

    All of it or none of it: a half-written run is indistinguishable from a complete one
    once the report is rendered from SQL, which is exactly the failure the store exists to
    prevent.
    """
    run_id = payload.run["run_id"]

    with db.transaction():
        # A published week is what readers are looking at; replacing its rows underneath
        # them would change a review nobody re-published (design section 7.10).
        if db.query_one(
            "SELECT 1 AS published FROM review_publications "
            "WHERE run_id = :run_id AND withdrawn_at IS NULL",
            {"run_id": run_id},
        ):
            raise RunIsPublished(
                f"run {run_id[:16]} is published as a weekly review; withdraw it with "
                "`alerts-bi unpublish` before re-persisting it"
            )

        # Replace this run's own rows. ON DELETE CASCADE covers the children, but they are
        # deleted explicitly so the intent survives a future schema change.
        for table in (
            "daily_metrics",
            "daily_rule_counts",
            "alert_findings",
            "llm_batch_attempts",
            "run_panels",
        ):
            db.execute(f"DELETE FROM {table} WHERE run_id = :run_id", {"run_id": run_id})

        # The run row itself is updated in place rather than deleted: a withdrawn
        # publication still references it, and that audit row must survive a rerun.
        run_values = {name: payload.run.get(name) for name in _RUN_COLUMNS}
        if db.query_one("SELECT 1 AS present FROM runs WHERE run_id = :run_id", {"run_id": run_id}):
            assignments = ", ".join(
                f"{name} = :{name}" for name in _RUN_COLUMNS if name != "run_id"
            )
            db.execute(f"UPDATE runs SET {assignments} WHERE run_id = :run_id", run_values)
        else:
            db.execute(_insert_statement("runs", _RUN_COLUMNS), run_values)

        db.execute_many(
            _insert_statement("daily_metrics", _DAILY_METRIC_COLUMNS),
            _project(payload.daily_metrics, _DAILY_METRIC_COLUMNS),
        )
        db.execute_many(
            _insert_statement("daily_rule_counts", _RULE_COUNT_COLUMNS),
            _project(payload.rule_counts, _RULE_COUNT_COLUMNS),
        )
        db.execute_many(
            _insert_statement("alert_findings", _FINDING_COLUMNS),
            _project(payload.findings, _FINDING_COLUMNS),
        )
        db.execute_many(
            _insert_statement("llm_batch_attempts", _BATCH_ATTEMPT_COLUMNS),
            _project(payload.batch_attempts, _BATCH_ATTEMPT_COLUMNS),
        )
        db.execute_many(
            _insert_statement("run_panels", _RUN_PANEL_COLUMNS),
            _project(payload.run_panels, _RUN_PANEL_COLUMNS),
        )

        for verdict in payload.verdicts:
            insert_verdict_if_absent(db, verdict)
        for parse in payload.panel_parses:
            insert_panel_parse_if_absent(db, parse)


def insert_verdict_if_absent(db: Database, verdict: dict[str, Any]) -> None:
    """Insert a verdict only when its cache key is not already present.

    A stored verdict belongs to the prompt and model version that produced it and is never
    recomputed under the same pair, so an existing row always wins.
    """
    names = ", ".join(_VERDICT_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in _VERDICT_COLUMNS)
    db.execute(
        f"""
        IF NOT EXISTS (
          SELECT 1 FROM llm_verdicts
          WHERE application = :application AND key_field = :key_field
            AND prompt_version = :prompt_version AND model_version = :model_version
        )
        INSERT INTO llm_verdicts ({names}) VALUES ({placeholders})
        """,
        {name: verdict.get(name) for name in _VERDICT_COLUMNS},
    )


def insert_panel_parse_if_absent(db: Database, parse: dict[str, Any]) -> None:
    names = ", ".join(_PANEL_PARSE_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in _PANEL_PARSE_COLUMNS)
    db.execute(
        f"""
        IF NOT EXISTS (
          SELECT 1 FROM panel_parses
          WHERE sql_text_hash = :sql_text_hash AND parser_version = :parser_version
        )
        INSERT INTO panel_parses ({names}) VALUES ({placeholders})
        """,
        {name: parse.get(name) for name in _PANEL_PARSE_COLUMNS},
    )


def find_verdicts(
    db: Database,
    prompt_version: str,
    model_version: str,
    keys: Sequence[tuple[str, str]],
) -> dict[str, dict[str, Any]]:
    """Durable verdict lookup for one prompt/model pair.

    Section 3.3 measured that keys do not recur across days, so the real work happens
    within a run; a cross-run hit is a bonus from overlapping windows, never a saving to
    plan around.
    """
    found: dict[str, dict[str, Any]] = {}
    if not keys:
        return found

    chunk_size = 400
    for start in range(0, len(keys), chunk_size):
        chunk = keys[start : start + chunk_size]
        params: dict[str, Any] = {
            "prompt_version": prompt_version,
            "model_version": model_version,
        }
        predicates = []
        for index, (application, key_field) in enumerate(chunk):
            params[f"app_{index}"] = application
            params[f"key_{index}"] = key_field
            predicates.append(f"(application = :app_{index} AND key_field = :key_{index})")

        rows = db.query(
            f"""
            SELECT * FROM llm_verdicts
            WHERE prompt_version = :prompt_version AND model_version = :model_version
              AND ({" OR ".join(predicates)})
            """,
            params,
        )
        for row in rows:
            found[verdict_key(str(row["application"]), str(row["key_field"]))] = row
    return found


def find_panel_parse(
    db: Database, sql_text_hash: str, parser_version: str
) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM panel_parses WHERE sql_text_hash = :hash AND parser_version = :version",
        {"hash": sql_text_hash, "version": parser_version},
    )
