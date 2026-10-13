"""`docs/outputs.md` describes what a run emits, and has to keep describing it.

Documentation drifts silently: a column added to an export is invisible to every other test
here, and a reference that has quietly stopped matching the code is worse than none, because
it is trusted. These checks fail when the document falls behind the thing it documents.

They assert coverage and the invariants a reader could get wrong, not prose.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from alerts_bi_operations.report.csv_export import (
    DAILY_METRIC_HEADERS,
    RULE_COUNT_HEADERS,
    WORKLIST_HEADERS,
)
from alerts_bi_operations.report.render import OUTPUT_FILES

_DOC_PATH = Path(__file__).resolve().parents[3] / "docs" / "upstream" / "outputs.md"
DOC = _DOC_PATH.read_bytes().decode("utf-8")

#: Prose wraps, so a sentence the document does state can straddle a line break. Phrases are
#: matched against this rather than the raw text; column and section names against the raw.
FLAT = re.sub(r"\s+", " ", DOC)

EXPORTS = {
    "daily_metrics.csv": DAILY_METRIC_HEADERS,
    "rule_counts.csv": RULE_COUNT_HEADERS,
    "alert_worklist.csv": WORKLIST_HEADERS,
}


def _columns(export: str) -> list[tuple[str, str]]:
    return [(export, column) for column in EXPORTS[export]]


ALL_COLUMNS = [pair for export in EXPORTS for pair in _columns(export)]


# ------------------------------------------------------------------ coverage


@pytest.mark.parametrize(("export", "column"), ALL_COLUMNS, ids=lambda v: str(v))
def test_every_exported_column_is_described(export: str, column: str) -> None:
    assert f"`{column}`" in DOC, f"{export} exports {column!r} and the document never mentions it"


@pytest.mark.parametrize("name", OUTPUT_FILES)
def test_every_output_file_is_described(name: str) -> None:
    assert f"`{name}`" in DOC


@pytest.mark.parametrize(
    "state", ["rule_flagged", "llm_flagged", "needs_review", "assessed_good", "unassessed"]
)
def test_every_quality_state_is_described(state: str) -> None:
    assert f"`{state}`" in DOC


@pytest.mark.parametrize("column_count", [len(headers) for headers in EXPORTS.values()])
def test_the_stated_column_counts_are_the_real_ones(column_count: int) -> None:
    """The document names a count per export; a stale one misleads more than no count."""
    assert f"{column_count} columns" in DOC


# ---------------------------------------------------- the invariants it teaches


@pytest.mark.parametrize(
    "claim",
    [
        # Each of these is a reading a user or an agent would otherwise get wrong.
        "subset of `flagged_by_rule`",
        "never as a window total",
        "never added together",
        "never inferred by subtraction",
        "not zero",
        "double-counts",
        "mutually exclusive",
    ],
)
def test_the_document_states_the_invariant_that_stops_a_misreading(claim: str) -> None:
    assert claim in FLAT, f"the document no longer explains: {claim!r}"


def test_the_document_defers_to_the_design_rather_than_competing_with_it() -> None:
    """Two documents claiming to be canonical is how a spec starts drifting."""
    assert "alerts_bi_design.md" in DOC
    assert "the design wins" in DOC


def test_the_severity_scale_is_stated_for_both_schemas() -> None:
    for name in ("error", "critical", "major", "high", "warning", "clear"):
        assert f"`{name}`" in DOC
