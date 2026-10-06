"""Read-only schema readiness for the operator app and its report/decision operations."""

from __future__ import annotations

from alerts_bi_shared.db.connection import Database

# The application reads these tables directly, including the SQL-only report renderer.
# Keep explicit columns: SELECT * would compile against an older table missing a field
# that the renderer subsequently expects in its result. TOP 0 never fetches stored data.
REQUIRED_COLUMNS: dict[str, str] = {
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
