"""CSV export (design section 6, "Exact MVP output contract").

Exactly three files: ``daily_metrics.csv``, ``rule_counts.csv`` and
``alert_worklist.csv``. Ordering is deterministic, CSV carries full values even where the
HTML shortens display text, and spreadsheet-formula prefixes are neutralized.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from alerts_bi_shared.timefmt import iso_date, iso_instant

__all__ = [
    "alert_worklist_csv",
    "csv_cell",
    "daily_metrics_csv",
    "rule_counts_csv",
    "to_csv",
]

#: Cells beginning with these are interpreted as formulas by spreadsheet software.
_FORMULA_PREFIXES: Final = ("=", "+", "-", "@")


def csv_cell(value: Any) -> str:
    """Render one CSV cell.

    A cell whose text begins with ``=``, ``+``, ``-`` or ``@`` is prefixed with a single
    quote: alert messages and node names are free text written by other teams, and a value
    such as ``=cmd|' /c calc'!A0`` reaching a spreadsheet is a code-execution path, not a
    formatting quirk. The escape is visible in the cell rather than silently altering the
    value.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        text = iso_instant(value)
    elif isinstance(value, date):
        text = value.isoformat()
    elif isinstance(value, Decimal):
        text = format(value.normalize(), "f")
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)

    if text.startswith(_FORMULA_PREFIXES):
        text = "'" + text
    return text


def to_csv(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """Render rows as CSV text with CRLF line endings (RFC 4180)."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([csv_cell(header) for header in headers])
    for row in rows:
        writer.writerow([csv_cell(cell) for cell in row])
    return buffer.getvalue()


def _iso_date(value: Any) -> str:
    if isinstance(value, datetime):
        return iso_date(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")[:10]


def _bit(value: Any) -> bool | None:
    """A SQL ``BIT`` read back as 0/1 becomes ``true``/``false``; NULL stays empty."""
    return None if value is None else bool(value)


DAILY_METRIC_HEADERS: Final = (
    "run_id",
    "team_id",
    "schema",
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


def daily_metrics_csv(daily_metrics: Sequence[Mapping[str, Any]]) -> str:
    """``daily_metrics.csv`` - one row per schema and UTC date."""
    rows = [
        [
            row["run_id"],
            row["team_id"],
            row["alert_schema"],
            _iso_date(row["snapshot_date"]),
            row["bucket_start"],
            row["bucket_end"],
            row["covered_hours"],
            row["alerts"],
            row["distinct_alerts"],
            row["alerts_per_hour"],
            row["node_name_numerator"],
            row["node_name_denominator"],
            row["node_name_ratio"],
            row["key_inflation_numerator"],
            row["key_inflation_denominator"],
            row["key_inflation_ratio"],
            row["flagged_by_rule"],
            row["flagged_by_rule_distinct"],
            row["flagged_by_llm"],
            row["flagged_by_llm_distinct"],
            row["needs_review"],
            row["assessed_good"],
            row["unassessed"],
            row["phase2_gaps"],
            row["suppressed"],
            row["suppression_unmeasured"],
            row.get("unseen"),
            row.get("unseen_unmeasured"),
        ]
        for row in daily_metrics
    ]
    return to_csv(DAILY_METRIC_HEADERS, rows)


RULE_COUNT_HEADERS: Final = (
    "run_id",
    "team_id",
    "schema",
    "snapshot_date",
    "rule_id",
    "ruleset_version",
    "match_count",
    "distinct_count",
)


def rule_counts_csv(rule_counts: Sequence[Mapping[str, Any]]) -> str:
    """``rule_counts.csv`` - the per-rule daily breakdown that ``flagged`` drills into."""
    rows = [
        [
            row["run_id"],
            row["team_id"],
            row["alert_schema"],
            _iso_date(row["snapshot_date"]),
            row["rule_id"],
            row["ruleset_version"],
            row["match_count"],
            row["distinct_count"],
        ]
        for row in rule_counts
    ]
    return to_csv(RULE_COUNT_HEADERS, rows)


WORKLIST_HEADERS: Final = (
    "run_id",
    "schema",
    "application",
    "key_field",
    "quality_state",
    "core_rule_ids",
    "readiness_rule_ids",
    "llm_principle_id",
    "llm_confidence",
    "llm_justification",
    "unassessed_reason",
    "row_count",
    "first_seen",
    "last_seen",
    "severity",
    "component",
    "node_name",
    "environment",
    "provider",
    "alert_rule_url",
    "message",
    "clear_count",
    "max_clear_cycles_24h",
    "fire_pattern",
    "unseen",
    "max_episode_firing_rows",
    "open_since",
)


def alert_worklist_csv(findings: Sequence[Mapping[str, Any]]) -> str:
    """``alert_worklist.csv`` - one row per distinct identity, never divided by seven.

    This is the concrete list of what to fix that the BI promises each measured team, so it
    carries full untruncated values.
    """
    rows = [
        [
            row["run_id"],
            row["alert_schema"],
            row["application"],
            row["key_field"],
            row["quality_state"],
            row["core_rule_ids"],
            row["readiness_rule_ids"],
            row["llm_principle_id"],
            row["llm_confidence"],
            row["llm_justification"],
            row["unassessed_reason"],
            row["row_count"],
            row["first_seen"],
            row["last_seen"],
            row["severity"],
            row["component"],
            row["node_name"],
            row["environment"],
            row["provider"],
            row["alert_rule_url"],
            row["message"],
            row.get("clear_count", 0),
            row.get("max_clear_cycles_24h", 0),
            row.get("fire_pattern"),
            _bit(row.get("unseen")),
            row.get("max_episode_firing_rows", 0),
            row.get("open_since"),
        ]
        for row in findings
    ]
    return to_csv(WORKLIST_HEADERS, rows)
