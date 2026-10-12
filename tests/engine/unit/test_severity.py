"""Severity is stored as a number and read as a name.

The number alone does not identify the level: 5 is ``error`` in v1 and ``critical`` in v2,
and those are different statements - R9 makes a missing runbook block phase-2 completion on
``critical`` and merely visible on anything else. Every test here exists because getting the
schema wrong would quietly change what a team is told.
"""

from __future__ import annotations

import pytest
from src.engine.domain.severity import (
    SEVERITY_CODES,
    SEVERITY_NAMES,
    code_for_name,
    severity_label,
)
from src.engine.rules.readiness import evaluate_r9, is_completion_ready

from tests.engine.helpers.rows import v1_row, v2_row

# ------------------------------------------------------------------ the scale


@pytest.mark.parametrize(
    ("code", "v1_name", "v2_name"),
    [
        (5, "error", "critical"),
        (4, "major", "high"),
        (3, "warning", "warning"),
        (1, "clear", "clear"),
    ],
)
def test_each_code_reads_as_the_name_its_schema_gives_it(
    code: int, v1_name: str, v2_name: str
) -> None:
    assert severity_label("v1", code) == v1_name
    assert severity_label("v2", code) == v2_name


def test_the_scale_defines_exactly_these_four_levels() -> None:
    """2 and anything above 5 are not levels the standard names."""
    assert sorted(SEVERITY_NAMES) == [1, 3, 4, 5]


@pytest.mark.parametrize("name", sorted(SEVERITY_CODES))
def test_every_name_round_trips_through_its_own_schema(name: str) -> None:
    code = code_for_name(name)
    schema = "v2" if name in ("critical", "high") else "v1"
    assert severity_label(schema, code) == name


def test_both_vocabularies_are_accepted_by_name() -> None:
    assert code_for_name("critical") == code_for_name("error") == 5
    assert code_for_name("high") == code_for_name("major") == 4


@pytest.mark.parametrize("name", ["CRITICAL", " Warning ", "Clear"])
def test_a_name_is_matched_regardless_of_case_or_padding(name: str) -> None:
    assert code_for_name(name) in SEVERITY_NAMES


def test_a_name_outside_the_standard_is_refused_rather_than_invented() -> None:
    """A fixture asking for a level that does not exist must fail the build."""
    with pytest.raises(ValueError, match="unknown severity name"):
        code_for_name("catastrophic")


# ------------------------------------------------------- values it will not judge


def test_an_undefined_code_is_kept_rather_than_guessed_at() -> None:
    """Keeping 7 preserves what a team sent; nulling it would discard a real value."""
    assert severity_label("v1", 7) == "7"
    assert severity_label("v2", 2) == "2"


def test_an_absent_severity_stays_absent() -> None:
    assert severity_label("v1", None) is None
    assert severity_label("v2", "") is None
    assert severity_label("v2", "   ") is None


def test_digits_arriving_as_text_are_still_read_as_a_code() -> None:
    assert severity_label("v2", "5") == "critical"


def test_a_source_still_sending_a_name_is_passed_through_unchanged() -> None:
    """Tolerated so the pipeline stays readable during a transition."""
    assert severity_label("v2", "critical") == "critical"
    assert severity_label("v1", "error") == "error"


def test_a_boolean_is_never_read_as_a_severity_code() -> None:
    """bool is an int subclass, so True would otherwise read as code 1, "clear"."""
    assert severity_label("v1", True) == "True"


# ------------------------------------------------- what the rules see downstream


def test_normalization_turns_the_stored_number_into_the_name_rules_use() -> None:
    assert v1_row(severity=5).severity == "error"
    assert v2_row(severity=5).severity == "critical"


def test_a_v2_five_blocks_phase_two_completion_when_its_runbook_is_missing() -> None:
    """5 in v2 is critical, and only critical blocks completion."""
    row = v2_row(severity=5, runbook_url=None)
    assert evaluate_r9(row) is not None
    assert is_completion_ready(row) is False


@pytest.mark.parametrize("code", [4, 3])
def test_a_lesser_v2_severity_stays_visible_without_blocking_completion(code: int) -> None:
    row = v2_row(severity=code, runbook_url=None)
    assert evaluate_r9(row) is not None, "the gap is still reported"
    assert is_completion_ready(row) is True, "but it does not reduce the percentage"


def test_a_v1_five_is_error_and_never_reaches_the_v2_readiness_rules() -> None:
    """R8-R10 are v2-only, which is what keeps 5 unambiguous where it matters."""
    row = v1_row(severity=5)
    assert row.severity == "error"
    assert evaluate_r9(row) is None
