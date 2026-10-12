"""Test-only row builders.

``time_created`` defaults to the row's own ``@timestamp`` rather than to a fixed instant.
Otherwise moving ``@timestamp`` in a test would make R7 fire as a side effect, and a test
about some other rule would silently be testing R7 as well. A test that wants an invalid
timestamp sets ``time_created`` explicitly.
"""

from __future__ import annotations

from typing import Any

from src.engine.domain.normalize import AlertRecord, normalize_row

__all__ = ["v1_row", "v2_row"]

_UNSET = object()

_V1_DEFAULTS: dict[str, Any] = {
    "application": "app-1",
    "object": "component-1",
    "message": "Payment authorization error rate above 3% over 5m",
    "severity": "error",
    "operator": "team-op",
    "key_field": "app-1:component-1:node-1",
    "node_name": "node-1",
    "network": None,
    "alert_rule_url": "https://grafana.internal/d/rule-1",
    "provider": "grafana",
    "@timestamp": "2026-08-20T12:00:00.000Z",
}

_V2_DEFAULTS: dict[str, Any] = {
    "application": "app-2",
    "component": "component-2",
    "message": "p99 latency above 900ms on the authorize endpoint",
    "severity": "high",
    "status": "firing",
    "impact": "Checkout feels slow to customers",
    "runbook_url": "https://runbooks.internal/app-2/latency",
    "environment": "production",
    "site": None,
    "operator": "team-op-v2",
    "key_field": "abcdef0123456789",
    "node_name": "node-2",
    "network": None,
    "alert_rule_url": "https://grafana.internal/d/rule-2",
    "provider": "grafana",
    "@timestamp": "2026-08-20T12:00:00.000Z",
}


def _build(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = {**defaults, **overrides}
    if "time_created" not in overrides:
        merged["time_created"] = merged["@timestamp"]
    return {k: v for k, v in merged.items() if v is not _UNSET}


def v1_row(**overrides: Any) -> AlertRecord:
    """Build a normalized v1 record from partial fields."""
    return normalize_row("v1", _build(_V1_DEFAULTS, overrides))


def v2_row(**overrides: Any) -> AlertRecord:
    """Build a normalized v2 record from partial fields."""
    return normalize_row("v2", _build(_V2_DEFAULTS, overrides))
