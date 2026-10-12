"""Core deterministic rules (design section 4).

Core rules apply to both schemas and are the only findings valid for reading v1 and v2
side by side.

Every rule is evaluated on every raw row, returns a finding or ``None``, and carries
evidence naming exactly what matched - a team disputing a number must be able to see
"rule 2 matched the literal string ``i am alive``".

R5 (self-suppression) is not here: it comes from the panel-suppression evaluator, which
needs the team's panels and the owned row set, not a single row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from alerts_bi_shared.catalogs import (
    PLACEHOLDER_VALUES,
    R1_GENERIC_MESSAGES,
    R2_HEARTBEAT_MESSAGES,
)
from alerts_bi_shared.timefmt import iso_instant

from ..domain.normalize import AlertRecord
from .text import is_blank, normalize_field_value, normalize_message

__all__ = [
    "Finding",
    "evaluate_core_rules",
    "evaluate_r1",
    "evaluate_r2",
    "evaluate_r3",
    "evaluate_r4",
    "evaluate_r7",
]

#: The R7 validity interval.
_TWENTY_FOUR_HOURS = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class Finding:
    rule_id: str
    set: str
    """``core`` or ``v2_readiness``."""
    evidence: dict[str, Any] = field(default_factory=dict)
    """Heterogeneous by rule: each rule names exactly what matched."""


def evaluate_r1(row: AlertRecord) -> Finding | None:
    """R1 - generic message matching the versioned catalogue.

    Whole-message equality only. ``Disk full`` and ``OOM`` are short but potentially
    meaningful and continue to the LLM; length is never evidence.
    """
    normalized = normalize_message(row.message)
    if normalized is None or normalized not in R1_GENERIC_MESSAGES:
        return None
    return Finding("R1", "core", {"field": "message", "normalized": normalized})


def evaluate_r2(row: AlertRecord) -> Finding | None:
    """R2 - informational / heartbeat message matching the versioned catalogue.

    Also whole-message: ``backup completed with 10 failures`` and ``service is not
    healthy`` are not heartbeats and continue through the remaining checks.
    """
    normalized = normalize_message(row.message)
    if normalized is None or normalized not in R2_HEARTBEAT_MESSAGES:
        return None
    return Finding("R2", "core", {"field": "message", "normalized": normalized})


def evaluate_r3(row: AlertRecord) -> Finding | None:
    """R3 - placeholder or missing required identity/ownership metadata.

    Required on both schemas: ``application``, ``operator``, and the schema's component
    field. ``node_name`` is optional and is checked only when it is supplied - an absent
    or empty node name is valid, and flagging it would penalise every alert that
    legitimately has no node scope.
    """
    component_field = "object" if row.schema == "v1" else "component"
    checks: list[tuple[str, Any, bool]] = [
        ("application", row.application, True),
        ("operator", row.operator, True),
        (component_field, row.component, True),
        ("node_name", row.node_name, False),
    ]

    violations: list[dict[str, str]] = []
    for name, value, required in checks:
        if is_blank(value):
            # Empty matches only on required fields; an absent optional node_name is valid.
            if required:
                violations.append({"field": name, "reason": "empty"})
            continue
        normalized = normalize_field_value(value)
        if normalized is not None and normalized in PLACEHOLDER_VALUES:
            violations.append({"field": name, "reason": "placeholder", "normalized": normalized})

    if not violations:
        return None
    return Finding("R3", "core", {"violations": violations})


def evaluate_r4(row: AlertRecord) -> Finding | None:
    """R4 - a Grafana alert with no alert-rule link.

    Scoped to ``provider = grafana`` only. API alerts do not carry ``alert_rule_url`` at
    all, so its absence is not evidence against them; they continue to the LLM and fall
    back to application grouping for batching.
    """
    if normalize_field_value(row.provider) != "grafana":
        return None
    if not is_blank(row.alert_rule_url):
        return None
    return Finding(
        "R4",
        "core",
        {"provider": row.provider, "alert_rule_url": row.alert_rule_url},
    )


def _parse_time_created(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def evaluate_r7(row: AlertRecord) -> Finding | None:
    """R7 - invalid v1 ``time_created``.

    ``@timestamp`` is the receipt time. The valid interval is
    ``[@timestamp - 24h, @timestamp]``, INCLUSIVE at both ends; a value later than receipt
    or older than 24 hours at receipt is flagged.

    v1 requires ``time_created``, so the rule checks validity rather than presence - but
    an absent or unparseable value cannot fall inside the interval and is not valid. That
    also matches characteristic 7 of the standard, which calls a missing event timestamp a
    bad alert outright (design section 7.6).

    v2 stamps this field itself, which is why the rule is v1-only by construction.
    """
    if row.schema != "v1":
        return None

    receipt = row.timestamp
    earliest = receipt - _TWENTY_FOUR_HOURS

    if is_blank(row.time_created):
        return Finding("R7", "core", {"reason": "missing", "time_created": row.time_created})

    created = _parse_time_created(str(row.time_created))
    if created is None:
        return Finding("R7", "core", {"reason": "unparseable", "time_created": row.time_created})
    if created > receipt:
        return Finding(
            "R7",
            "core",
            {
                "reason": "future",
                "time_created": row.time_created,
                "timestamp": iso_instant(receipt),
            },
        )
    if created < earliest:
        return Finding(
            "R7",
            "core",
            {
                "reason": "older_than_24h",
                "time_created": row.time_created,
                "timestamp": iso_instant(receipt),
            },
        )
    return None


def evaluate_core_rules(row: AlertRecord) -> list[Finding]:
    """Evaluate every core rule that a single row can decide on its own.

    R5 is added afterwards by the suppression evaluator, which is why it is absent here.
    """
    findings: list[Finding] = []
    for rule in (evaluate_r1, evaluate_r2, evaluate_r3, evaluate_r4, evaluate_r7):
        finding = rule(row)
        if finding is not None:
            findings.append(finding)
    return findings
