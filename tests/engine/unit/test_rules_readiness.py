from __future__ import annotations

import pytest
from alerts_bi_shared.phase import derive_phase
from src.engine.rules.readiness import (
    evaluate_r8,
    evaluate_r9,
    evaluate_r10,
    is_completion_ready,
    phase2_readiness_pct,
)

from tests.engine.helpers.rows import v1_row, v2_row

# ------------------------------------------------------------------------- R8


@pytest.mark.parametrize(
    ("impact", "reason"),
    [(None, "missing"), (42, "not_a_string"), ("   ", "empty")],
)
def test_r8_flags_a_missing_non_string_or_empty_impact(impact: object, reason: str) -> None:
    finding = evaluate_r8(v2_row(impact=impact))
    assert finding is not None
    assert finding.evidence["reason"] == reason


@pytest.mark.parametrize("value", ["Unknown", "Test", "Default", "N/A"])
def test_r8_flags_an_exact_placeholder_impact(value: str) -> None:
    finding = evaluate_r8(v2_row(impact=value))
    assert finding is not None
    assert finding.evidence["reason"] == "placeholder"


def test_r8_does_not_flag_a_present_but_poor_impact() -> None:
    assert evaluate_r8(v2_row(impact="high cpu")) is None


def test_r8_never_applies_to_v1() -> None:
    assert evaluate_r8(v1_row()) is None


# ------------------------------------------------------------------------- R9


@pytest.mark.parametrize(
    ("url", "reason"),
    [(None, "missing"), (7, "not_a_string"), ("  ", "empty"), ("N/A", "placeholder")],
)
def test_r9_flags_a_missing_non_string_empty_or_placeholder_runbook(
    url: object, reason: str
) -> None:
    finding = evaluate_r9(v2_row(runbook_url=url))
    assert finding is not None
    assert finding.evidence["reason"] == reason


@pytest.mark.parametrize(
    "bad",
    [
        "/runbooks/local-path",
        "runbooks.internal/x",
        "ftp://runbooks.internal/x",
        "mailto:oncall@example.com",
        "https://",
        "//runbooks.internal/x",
    ],
)
def test_r9_requires_an_absolute_http_url_naming_a_host(bad: str) -> None:
    finding = evaluate_r9(v2_row(runbook_url=bad))
    assert finding is not None
    assert finding.evidence["reason"] == "not_absolute_http"


@pytest.mark.parametrize(
    "good",
    [
        "https://runbooks.internal/a/b?x=1#y",
        "http://runbooks.internal/a",
        "https://user:pass@host/path",
        "http://host:8080/path",
        # WHATWG tolerates extra or missing slashes for special schemes, and the
        # superseded JavaScript implementation accepted these. Preserved deliberately.
        "http:///no-host",
        "http:runbook",
        "https:/single-slash",
    ],
)
def test_r9_accepts_urls_the_javascript_implementation_accepted(good: str) -> None:
    assert evaluate_r9(v2_row(runbook_url=good)) is None


def test_r9_is_reported_for_every_severity_but_blocks_only_critical() -> None:
    critical = evaluate_r9(v2_row(severity="critical", runbook_url=None))
    assert critical is not None
    assert critical.evidence["blocks_completion"] is True
    for severity in ("high", "warning"):
        finding = evaluate_r9(v2_row(severity=severity, runbook_url=None))
        assert finding is not None, severity
        assert finding.evidence["blocks_completion"] is False


def test_r9_never_applies_to_v1() -> None:
    assert evaluate_r9(v1_row()) is None


# ------------------------------------------------------------------------ R10


@pytest.mark.parametrize(
    "value", ["high cpu", "High CPU usage", "CPU usage is high", "cpu is high"]
)
def test_r10_matches_only_the_exact_technical_cause_catalogue(value: str) -> None:
    assert evaluate_r10(v2_row(impact=value)) is not None


def test_r10_does_not_substring_match_a_causal_sentence() -> None:
    assert evaluate_r10(v2_row(impact="high cpu causes checkout latency")) is None
    assert evaluate_r10(v2_row(impact="Customers cannot complete checkout")) is None


def test_r10_never_applies_to_v1_and_ignores_a_non_string_impact() -> None:
    assert evaluate_r10(v1_row()) is None
    assert evaluate_r10(v2_row(impact=42)) is None


# ------------------------------------------------- phase-2 completion readiness


def test_an_identity_with_impact_runbook_and_a_real_symptom_is_completion_ready() -> None:
    assert is_completion_ready(v2_row()) is True


def test_an_r8_or_r10_gap_makes_an_identity_not_ready_at_any_severity() -> None:
    assert is_completion_ready(v2_row(severity="warning", impact=None)) is False
    assert is_completion_ready(v2_row(severity="warning", impact="high cpu")) is False


def test_a_missing_runbook_blocks_completion_only_for_critical() -> None:
    assert is_completion_ready(v2_row(severity="critical", runbook_url=None)) is False
    assert is_completion_ready(v2_row(severity="high", runbook_url=None)) is True
    assert is_completion_ready(v2_row(severity="warning", runbook_url=None)) is True


def test_readiness_is_only_defined_for_v2() -> None:
    with pytest.raises(TypeError, match="only defined for v2"):
        is_completion_ready(v1_row())


def test_phase2_readiness_is_ready_identities_over_all_v2_identities() -> None:
    reps = [
        v2_row(key_field="a"),
        v2_row(key_field="b"),
        v2_row(key_field="c", impact=None),
        v2_row(key_field="d", severity="critical", runbook_url=None),
    ]
    assert phase2_readiness_pct(reps) == 50


def test_phase2_readiness_is_none_with_no_v2_identities_never_zero() -> None:
    assert phase2_readiness_pct([]) is None


def test_a_non_critical_missing_runbook_stays_visible_but_does_not_reduce_readiness() -> None:
    assert phase2_readiness_pct([v2_row(severity="high", runbook_url=None)]) == 100
    assert evaluate_r9(v2_row(severity="high", runbook_url=None)) is not None


# ------------------------------------------------------------ phase derivation


def test_phase_derivation_covers_every_branch_exhaustively() -> None:
    assert derive_phase(0, 0, None) == "no_data"
    assert derive_phase(5, 0, None) == "phase_0"
    assert derive_phase(5, 3, 40) == "phase_1"
    assert derive_phase(0, 3, 40) == "phase_2"
    assert derive_phase(0, 3, 100) == "done"


def test_an_empty_window_is_no_data_not_phase_0() -> None:
    assert derive_phase(0, 0, None) == "no_data"


def test_v1_reaching_zero_does_not_mark_a_team_done_while_readiness_is_incomplete() -> None:
    assert derive_phase(0, 10, 99.9) == "phase_2"


def test_phase_1_is_both_schemas_present_regardless_of_readiness() -> None:
    assert derive_phase(1, 1, 100) == "phase_1"
    assert derive_phase(1, 1, 0) == "phase_1"
