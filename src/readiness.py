"""Read-only schema readiness for the operator app and its report/decision operations."""

from __future__ import annotations

from alerts_bi_shared.db.connection import Database

# The application reads these tables directly, including the SQL-only report renderer.
# Keep explicit columns: SELECT * would compile against an older table missing a field
# that the renderer subsequently expects in its result. TOP 0 never fetches stored data.
REQUIRED_COLUMNS: dict[str, str] = {
    "run_summaries": "run_id,scope_kind,application,payload",
    "run_alert_list": "finding_id,run_id,alert_schema,application,key_field,row_count,attention_rank,quality_state,core_rule_ids,readiness_rule_ids",
    "run_rule_membership": "run_id,finding_id,rule_id,matched_rows",
    "run_application_daily": "run_id,alert_schema,application,snapshot_date,covered_hours,events,distinct_alerts,flagged_rows,flagged_by_rule_distinct,suppressed,unseen",
    "run_v1_rules": "run_id,application,rule_hash",
    "run_analysis_checkpoints": "run_id,source_hash,format_version,payload",
    "run_jobs": (
        "job_id, idempotency_key, idempotency_key_hash, requested_by, request_hash, "
        "team_id, run_at, llm_mode, registry_sha256, run_id, status, active_slot, "
        "submitted_at, started_at, completed_at, error_summary, backfill_id"
    ),
    "run_backfills": (
        "backfill_id, team_id, requested_by, idempotency_key_hash, request_hash, llm_mode, "
        "week_count, submitted_at, cancelled_at, cancelled_by, dismissed_at"
    ),
    "runs": (
        "run_id, run_at, team_id, team_display_name, window_start, window_end, "
        "registry_version, registry_sha256, registry_entry_snapshot, ruleset_version, "
        "prompt_version, model_version, llm_assessed, phase_derived, phase2_readiness_pct, "
        "app_version, status, started_at, completed_at, error_summary"
    ),
    "daily_metrics": (
        "run_id, team_id, alert_schema, snapshot_date, bucket_start, bucket_end, covered_hours, "
        "alerts, distinct_alerts, alerts_per_hour, node_name_numerator, node_name_denominator, "
        "node_name_ratio, key_inflation_numerator, key_inflation_denominator, key_inflation_ratio, "
        "flagged_by_rule, flagged_by_rule_distinct, flagged_by_llm, flagged_by_llm_distinct, "
        "needs_review, assessed_good, unassessed, phase2_gaps, suppressed, suppression_unmeasured, "
        "unseen, unseen_unmeasured"
    ),
    "daily_rule_counts": (
        "run_id, team_id, alert_schema, snapshot_date, rule_id, ruleset_version, "
        "match_count, distinct_count"
    ),
    "alert_findings": (
        "run_id, alert_schema, application, key_field, representative_at, representative_hash, "
        "representative_doc, message, severity, component, node_name, environment, provider, "
        "alert_rule_url, row_count, first_seen, last_seen, core_rule_ids, readiness_rule_ids, "
        "findings_evidence, quality_state, llm_principle_id, llm_confidence, llm_justification, "
        "unassessed_reason, clear_count, max_clear_cycles_24h, fire_pattern, unseen, "
        "max_episode_firing_rows, open_since"
    ),
    "llm_batch_attempts": (
        "run_id, batch_id, attempt_number, group_type, group_value, partition_index, "
        "partition_count, alert_count, alert_ids, request_hash, request_payload, status, "
        "failure_reason, duration_ms, created_at"
    ),
    "run_panels": (
        "run_id, panel_id, alert_schema, sql_text_hash, parser_version, "
        "suppression_leaves, unmeasured_leaves, notes"
    ),
    "review_publications": (
        "publication_id, run_id, team_id, window_start, window_end, published_at, "
        "published_by, review_note, withdrawn_at, withdrawn_by, withdrawn_reason"
    ),
    "finding_decisions": (
        "decision_id, team_id, run_id, alert_schema, application, key_field, finding_id, "
        "state, note, decided_at, decided_by"
    ),
    "weekly_review_log": "log_id, invoked_at, team_id, window_end, outcome, run_id, detail",
}


def check_schema(db: Database) -> None:
    """Refuse readiness when an operator query needs unavailable tables or columns."""
    for table, columns in REQUIRED_COLUMNS.items():
        names = ", ".join(f"[{column.strip()}]" for column in columns.split(","))
        db.query(f"SELECT TOP 0 {names} FROM [{table}]")
    for version in (
        "009_run_deletion",
        "011_run_job_keys",
        "012_read_models",
        "013_analysis_checkpoints",
        "014_backfills",
        "015_case_sensitive_applications",
        "016_case_sensitive_key_fields",
        "017_case_sensitive_teams",
        "018_alert_identity_hash",
        "019_erase_operator_names",
    ):
        if (
            db.query_one(
                "SELECT 1 AS ready FROM schema_migrations WHERE version=:version",
                {"version": version},
            )
            is None
        ):
            raise ValueError(f"The operator schema needs migration {version}.")
