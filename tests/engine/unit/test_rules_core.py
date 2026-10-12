from __future__ import annotations

from typing import Any

import pytest
from src.engine.rules.core import (
    evaluate_core_rules,
    evaluate_r1,
    evaluate_r2,
    evaluate_r3,
    evaluate_r4,
    evaluate_r7,
)
from src.engine.rules.text import normalize_field_value, normalize_message

from tests.engine.helpers.rows import v1_row, v2_row

# ---------------------------------------------------------------- normalization


def test_message_normalization_trims_lowercases_collapses_and_strips_edge_punctuation() -> None:
    assert normalize_message("  Error   Occurred!  ") == "error occurred"
    assert normalize_message('"Something went wrong."') == "something went wrong"
    assert normalize_message("...OK...") == "ok"


def test_field_normalization_keeps_punctuation_because_n_slash_a_is_a_catalogue_value() -> None:
    # Interior punctuation survives both normalizations, so `n/a` stays matchable either way.
    assert normalize_field_value(" N/A ") == "n/a"
    assert normalize_message("N/A") == "n/a"
    # The two differ only at the edges, which is why R3 uses the field form.
    assert normalize_field_value("OK.") == "ok."
    assert normalize_message("OK.") == "ok"


def test_normalization_returns_none_for_a_non_string() -> None:
    assert normalize_message(42) is None
    assert normalize_field_value(None) is None


# ------------------------------------------------------------------------- R1


@pytest.mark.parametrize(
    "phrase",
    [
        "Error Occurred",
        "Something went wrong",
        "Unable to get data",
        "Alert triggered",
        "Issue detected",
    ],
)
def test_r1_matches_every_catalogue_phrase_as_a_whole_message(phrase: str) -> None:
    assert evaluate_r1(v1_row(message=phrase)) is not None


def test_r1_does_not_substring_match() -> None:
    assert evaluate_r1(v1_row(message="Error occurred while charging card 4242")) is None
    assert evaluate_r1(v1_row(message="No issue detected in the last hour")) is None


def test_r1_never_flags_on_length_alone() -> None:
    assert evaluate_r1(v1_row(message="Disk full")) is None
    assert evaluate_r1(v1_row(message="OOM")) is None


def test_r1_evidence_names_the_normalized_value_that_matched() -> None:
    finding = evaluate_r1(v1_row(message="  ERROR OCCURRED. "))
    assert finding is not None
    assert finding.rule_id == "R1"
    assert finding.set == "core"
    assert finding.evidence["normalized"] == "error occurred"


def test_r1_applies_to_both_schemas() -> None:
    assert evaluate_r1(v2_row(message="Alert triggered")) is not None


# ------------------------------------------------------------------------- R2


@pytest.mark.parametrize(
    "phrase",
    [
        "i am alive",
        "OK",
        "healthy",
        "started",
        "completed",
        "running",
        "service started",
        "process running",
        "completed successfully",
    ],
)
def test_r2_matches_every_heartbeat_catalogue_phrase(phrase: str) -> None:
    assert evaluate_r2(v1_row(message=phrase)) is not None


def test_r2_does_not_substring_match_the_documented_counter_examples() -> None:
    assert evaluate_r2(v1_row(message="backup completed with 10 failures")) is None
    assert evaluate_r2(v1_row(message="service is not healthy")) is None


# ------------------------------------------------------------------------- R3


@pytest.mark.parametrize("value", ["Unknown", "Test", "Default", "N/A"])
def test_r3_flags_an_exact_placeholder_in_a_required_field(value: str) -> None:
    finding = evaluate_r3(v1_row(operator=value))
    assert finding is not None
    assert finding.evidence["violations"][0]["field"] == "operator"
    assert finding.evidence["violations"][0]["reason"] == "placeholder"


def test_r3_matching_is_exact() -> None:
    assert evaluate_r3(v1_row(application="test-payments-service")) is None
    assert evaluate_r3(v1_row(operator="unknown-team-alpha")) is None


@pytest.mark.parametrize("field", [{"application": ""}, {"operator": "   "}, {"object": ""}])
def test_r3_flags_an_empty_required_field(field: dict[str, Any]) -> None:
    finding = evaluate_r3(v1_row(**field))
    assert finding is not None
    assert finding.evidence["violations"][0]["reason"] == "empty"


def test_r3_checks_component_on_v1_as_object_and_on_v2_as_component() -> None:
    v1 = evaluate_r3(v1_row(object="unknown"))
    v2 = evaluate_r3(v2_row(component="unknown"))
    assert v1 is not None and v1.evidence["violations"][0]["field"] == "object"
    assert v2 is not None and v2.evidence["violations"][0]["field"] == "component"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_r3_treats_an_absent_or_empty_node_name_as_valid(value: str | None) -> None:
    assert evaluate_r3(v1_row(node_name=value)) is None


def test_r3_flags_a_supplied_placeholder_node_name() -> None:
    finding = evaluate_r3(v1_row(node_name="Default"))
    assert finding is not None
    assert finding.evidence["violations"][0]["field"] == "node_name"


def test_r3_ignores_site_network_and_alert_rule_url() -> None:
    assert evaluate_r3(v2_row(site="unknown", network="test")) is None
    assert evaluate_r3(v2_row(alert_rule_url="default")) is None


def test_r3_reports_every_violating_field_not_just_the_first() -> None:
    finding = evaluate_r3(v1_row(application="unknown", operator=""))
    assert finding is not None
    assert len(finding.evidence["violations"]) == 2


# ------------------------------------------------------------------------- R4


@pytest.mark.parametrize("value", [None, "", "   "])
def test_r4_flags_a_grafana_alert_with_no_rule_url(value: str | None) -> None:
    assert evaluate_r4(v1_row(provider="grafana", alert_rule_url=value)) is not None


def test_r4_does_not_apply_to_api_alerts_even_with_no_rule_url() -> None:
    assert evaluate_r4(v1_row(provider="api", alert_rule_url=None)) is None
    assert evaluate_r4(v2_row(provider="api", alert_rule_url=None)) is None


def test_r4_does_not_flag_a_grafana_alert_that_has_a_rule_url() -> None:
    assert evaluate_r4(v1_row(provider="grafana", alert_rule_url="https://g/d/1")) is None


def test_r4_applies_to_both_schemas() -> None:
    assert evaluate_r4(v2_row(provider="grafana", alert_rule_url=None)) is not None


# ------------------------------------------------------------------------- R7

RECEIPT = "2026-08-20T12:00:00.000Z"


def test_r7_accepts_a_time_created_equal_to_the_timestamp() -> None:
    assert evaluate_r7(v1_row(**{"@timestamp": RECEIPT, "time_created": RECEIPT})) is None


def test_r7_accepts_a_time_created_exactly_24_hours_old() -> None:
    row = v1_row(**{"@timestamp": RECEIPT, "time_created": "2026-08-19T12:00:00.000Z"})
    assert evaluate_r7(row) is None


def test_r7_flags_a_time_created_one_millisecond_in_the_future() -> None:
    finding = evaluate_r7(
        v1_row(**{"@timestamp": RECEIPT, "time_created": "2026-08-20T12:00:00.001Z"})
    )
    assert finding is not None
    assert finding.evidence["reason"] == "future"


def test_r7_flags_a_time_created_one_millisecond_older_than_24_hours() -> None:
    finding = evaluate_r7(
        v1_row(**{"@timestamp": RECEIPT, "time_created": "2026-08-19T11:59:59.999Z"})
    )
    assert finding is not None
    assert finding.evidence["reason"] == "older_than_24h"


@pytest.mark.parametrize(
    ("value", "reason"),
    [(None, "missing"), ("   ", "missing"), ("yesterday", "unparseable")],
)
def test_r7_flags_a_missing_or_unparseable_time_created(value: str | None, reason: str) -> None:
    finding = evaluate_r7(v1_row(time_created=value))
    assert finding is not None
    assert finding.evidence["reason"] == reason


def test_r7_never_applies_to_v2_which_stamps_the_field_itself() -> None:
    assert evaluate_r7(v2_row(time_created="2030-01-01T00:00:00Z")) is None
    assert evaluate_r7(v2_row(time_created=None)) is None


# ------------------------------------------------------------- combined core


def test_a_row_can_carry_several_core_findings_at_once() -> None:
    findings = evaluate_core_rules(
        v1_row(
            message="i am alive",
            operator="unknown",
            provider="grafana",
            alert_rule_url=None,
            time_created=None,
        )
    )
    assert sorted(f.rule_id for f in findings) == ["R2", "R3", "R4", "R7"]


def test_a_clean_row_produces_no_core_findings() -> None:
    assert evaluate_core_rules(v1_row()) == []
    assert evaluate_core_rules(v2_row()) == []


def test_r5_is_never_produced_by_the_row_level_core_rules() -> None:
    findings = evaluate_core_rules(v1_row(message="i am alive", operator="unknown"))
    assert all(f.rule_id != "R5" for f in findings)


def test_r6_is_post_mvp_and_is_never_produced() -> None:
    for _ in range(50):
        assert all(f.rule_id != "R6" for f in evaluate_core_rules(v1_row()))
