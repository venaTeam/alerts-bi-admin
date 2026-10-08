"""Integration-test access to the disposable ``alerts_bi_test`` database.

Never the development or a production database: ``reset_test_database`` refuses any target
that is not the configured test database.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

__all__ = [
    "sample_daily",
    "sample_finding",
    "sample_run",
    "sample_verdict",
]


def _at(text: str) -> datetime:
    """SQL Server DATETIME2 columns take naive UTC values through this driver."""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)


def sample_run(**overrides: Any) -> dict[str, Any]:
    """Minimal valid run record for persistence tests."""
    return {
        "run_id": "a" * 64,
        "run_at": _at("2026-08-25T18:00:00Z"),
        "team_id": "checkout-api",
        "team_display_name": "Checkout API",
        "window_start": _at("2026-08-18T18:00:00Z"),
        "window_end": _at("2026-08-25T18:00:00Z"),
        "registry_version": "2026-08-30.1",
        "registry_sha256": "b" * 64,
        "registry_entry_snapshot": '{"team_id":"checkout-api"}',
        "ruleset_version": "1.0.0",
        "prompt_version": "1.0.0",
        "model_version": "fake-model-1",
        "llm_assessed": True,
        "phase_derived": "phase_1",
        "phase2_readiness_pct": 50,
        "app_version": "0.1.0",
        "status": "completed",
        "started_at": _at("2026-08-25T18:00:00Z"),
        "completed_at": _at("2026-08-25T18:00:05Z"),
        "error_summary": None,
        **overrides,
    }


def sample_daily(**overrides: Any) -> dict[str, Any]:
    """Minimal valid daily metric row."""
    return {
        "run_id": "a" * 64,
        "team_id": "checkout-api",
        "alert_schema": "v1",
        "snapshot_date": "2026-08-20",
        "bucket_start": _at("2026-08-20T00:00:00Z"),
        "bucket_end": _at("2026-08-21T00:00:00Z"),
        "covered_hours": 24,
        "alerts": 10,
        "distinct_alerts": 3,
        "alerts_per_hour": 10 / 24,
        "node_name_numerator": 3,
        "node_name_denominator": 1,
        "node_name_ratio": 3,
        "key_inflation_numerator": 3,
        "key_inflation_denominator": 1,
        "key_inflation_ratio": 3,
        "flagged_by_rule": 2,
        "flagged_by_rule_distinct": 1,
        "flagged_by_llm": 0,
        "flagged_by_llm_distinct": 0,
        "needs_review": 0,
        "assessed_good": 2,
        "unassessed": 0,
        "phase2_gaps": 0,
        "suppressed": 1,
        "suppression_unmeasured": 0,
        **overrides,
    }


def sample_finding(**overrides: Any) -> dict[str, Any]:
    """Minimal valid finding row."""
    return {
        "run_id": "a" * 64,
        "alert_schema": "v1",
        "application": "checkout-api",
        "key_field": "checkout-api:cart:node-1",
        "representative_at": _at("2026-08-20T12:00:00Z"),
        "representative_hash": "c" * 64,
        "representative_doc": '{"application":"checkout-api"}',
        "message": "Cart service error rate above 2%",
        "severity": "error",
        "component": "cart",
        "node_name": "node-1",
        "environment": None,
        "provider": "grafana",
        "alert_rule_url": "https://grafana.internal/d/checkout-1",
        "row_count": 4,
        "first_seen": _at("2026-08-20T09:00:00Z"),
        "last_seen": _at("2026-08-20T12:00:00Z"),
        "core_rule_ids": "",
        "readiness_rule_ids": "",
        "findings_evidence": "[]",
        "quality_state": "assessed_good",
        "llm_principle_id": "NONE",
        "llm_confidence": "high",
        "llm_justification": "Metric-based, names the component and the symptom.",
        "unassessed_reason": None,
        **overrides,
    }


def sample_verdict(**overrides: Any) -> dict[str, Any]:
    """Minimal valid verdict row."""
    return {
        "application": "checkout-api",
        "key_field": "checkout-api:cart:node-1",
        "prompt_version": "1.0.0",
        "model_version": "fake-model-1",
        "alert_schema": "v1",
        "assessment": "no_violation",
        "principle_id": "NONE",
        "confidence": "high",
        "justification": "Metric-based and actionable.",
        "representative_doc": '{"application":"checkout-api"}',
        "doc_hash": "c" * 64,
        "classified_at": _at("2026-08-25T18:00:01Z"),
        "ruleset_version": "1.0.0",
        "first_run_id": "a" * 64,
        **overrides,
    }
