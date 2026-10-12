from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from alerts_bi_shared.versions import PROMPT_VERSION as CURRENT_PROMPT_VERSION
from src.engine.db.repositories import verdict_key
from src.engine.llm.assess import MAX_ATTEMPTS, assess_alerts, mark_all_unassessed
from src.engine.llm.fake import FakeLlmClient, ScriptedResult, scripted_verdicts
from src.engine.llm.grouping import (
    MAX_BATCH_SIZE,
    alert_transport_id,
    balanced_partition_sizes,
    batch_id_of,
    build_batches,
    group_alerts,
)
from src.engine.llm.prompt import build_prompt
from src.engine.llm.request import (
    assert_lossless,
    build_request,
    reconstruct_document,
    serialize_request,
    shared_field_names,
)
from src.engine.llm.response import (
    LlmResponseError,
    response_json_schema,
    state_for_verdict,
    validate_response,
)

from tests.engine.helpers.rows import v1_row, v2_row

RUN_ID = "run-1"
PROMPT_VERSION = "1.0.0"
MODEL_VERSION = "fake-model-1"
NOW = datetime(2026, 8, 25, 18, 0, 0, tzinfo=UTC)


def batches(alerts: list[Any], **kwargs: Any) -> list[Any]:
    return build_batches(alerts, RUN_ID, PROMPT_VERSION, MODEL_VERSION, **kwargs)


# ------------------------------------------------------------------ grouping


def test_alerts_group_by_alert_rule_url_when_present() -> None:
    groups = group_alerts(
        [
            v1_row(key_field="a", alert_rule_url="https://g/d/1"),
            v1_row(key_field="b", alert_rule_url="https://g/d/1"),
            v1_row(key_field="c", alert_rule_url="https://g/d/2"),
        ]
    )
    assert len(groups) == 2
    assert groups[0].type == "alert_rule_url"
    assert len(groups[0].alerts) == 2


def test_alerts_with_no_rule_url_fall_back_to_application_grouping() -> None:
    groups = group_alerts(
        [
            v1_row(key_field="a", provider="api", alert_rule_url=None, application="app-x"),
            v1_row(key_field="b", provider="api", alert_rule_url="   ", application="app-x"),
        ]
    )
    assert len(groups) == 1
    assert groups[0].type == "application"
    assert groups[0].value == "app-x"


def test_rule_url_and_application_groups_never_merge() -> None:
    groups = group_alerts(
        [
            v1_row(key_field="a", alert_rule_url="https://g/d/1", application="app-x"),
            v1_row(key_field="b", alert_rule_url=None, application="app-x"),
        ]
    )
    assert len(groups) == 2


def test_small_groups_are_never_packed_together_to_fill_capacity() -> None:
    built = batches(
        [
            v1_row(key_field="a", alert_rule_url="https://g/d/1"),
            v1_row(key_field="b", alert_rule_url="https://g/d/2"),
            v1_row(key_field="c", alert_rule_url="https://g/d/3"),
        ]
    )
    assert [len(b.alerts) for b in built] == [1, 1, 1]


def test_grouping_and_ordering_are_deterministic_regardless_of_input_order() -> None:
    alerts = [
        v1_row(key_field="c", alert_rule_url="https://g/d/2"),
        v1_row(key_field="a", alert_rule_url="https://g/d/1"),
        v1_row(key_field="b", alert_rule_url="https://g/d/1"),
    ]
    forward = batches(alerts)
    reversed_ = batches(list(reversed(alerts)))
    assert [b.batch_id for b in forward] == [b.batch_id for b in reversed_]
    assert [a.key_field for a in forward[0].alerts] == ["a", "b"]


# ------------------------------------------------------ balanced partitions


def test_401_alerts_split_into_134_134_and_133() -> None:
    assert balanced_partition_sizes(401, 200) == [134, 134, 133]


def test_a_group_at_or_below_the_cap_is_exactly_one_batch() -> None:
    assert balanced_partition_sizes(200, 200) == [200]
    assert balanced_partition_sizes(1, 200) == [1]
    assert balanced_partition_sizes(0, 200) == []


@pytest.mark.parametrize("n", [201, 399, 400, 401, 599, 1000, 1234])
def test_partition_sizes_never_differ_by_more_than_one_and_sum_to_n(n: int) -> None:
    sizes = balanced_partition_sizes(n, 200)
    assert sum(sizes) == n
    assert max(sizes) - min(sizes) <= 1
    assert len(sizes) == -(-n // 200)
    assert max(sizes) <= 200


def test_a_group_above_the_cap_becomes_balanced_batches_carrying_every_alert_once() -> None:
    alerts = [v1_row(key_field=f"k{i:04d}", alert_rule_url="https://g/d/big") for i in range(401)]
    built = batches(alerts)
    assert [len(b.alerts) for b in built] == [134, 134, 133]
    assert all(b.partition_count == 3 for b in built)
    assert len({a.key_field for b in built for a in b.alerts}) == 401


def test_the_configured_batch_size_may_lower_the_ceiling_but_never_raise_it() -> None:
    alerts = [v1_row(key_field=f"k{i}", alert_rule_url="https://g/d/1") for i in range(10)]
    assert len(batches(alerts, max_batch_size=4)) == 3
    assert len(batches(alerts, max_batch_size=100000)) == 1
    assert MAX_BATCH_SIZE == 200


# ------------------------------------------------------------ transport ids


def test_the_alert_transport_id_is_derived_from_schema_application_and_key_field() -> None:
    a = alert_transport_id(v1_row(application="x", key_field="k"))
    b = alert_transport_id(v1_row(application="x", key_field="k", message="different"))
    c = alert_transport_id(v1_row(application="x", key_field="k2"))
    assert a == b, "message is not part of transport identity"
    assert a != c
    assert len(a) == 64


def test_the_same_alert_under_v1_and_v2_gets_different_transport_ids() -> None:
    assert alert_transport_id(v1_row(application="x", key_field="k")) != alert_transport_id(
        v2_row(application="x", key_field="k")
    )


def _batch_id(
    run_id: str = "r1",
    group_type: str = "alert_rule_url",
    group_value: str = "https://g/d/1",
    partition_index: int = 0,
    alert_ids: tuple[str, ...] = ("a", "b"),
    prompt_version: str = "1.0.0",
    model_version: str = "m1",
) -> str:
    return batch_id_of(
        run_id, group_type, group_value, partition_index, alert_ids, prompt_version, model_version
    )


def test_the_batch_id_covers_run_group_partition_membership_and_versions() -> None:
    original = _batch_id()
    assert _batch_id() == original, "identical input gives an identical id"
    assert _batch_id(run_id="r2") != original
    assert _batch_id(group_value="https://g/d/2") != original
    assert _batch_id(partition_index=1) != original
    assert _batch_id(alert_ids=("b", "a")) != original, "ordering is part of identity"
    assert _batch_id(prompt_version="2.0.0") != original
    assert _batch_id(model_version="m2") != original


# ------------------------------------------------------------ payload factoring


def test_only_fields_identical_across_the_whole_batch_are_factored_out() -> None:
    batch = batches(
        [
            v1_row(key_field="a", alert_rule_url="https://g/d/1", object="same", message="one"),
            v1_row(key_field="b", alert_rule_url="https://g/d/1", object="same", message="two"),
        ]
    )[0]
    request = build_request(batch, "1.0.0", PROMPT_VERSION)

    assert request["shared_fields"]["object"] == "same"
    assert "message" not in request["shared_fields"], "differing fields stay per-alert"
    assert request["alerts"][0]["fields"]["message"] == "one"


def test_grouping_by_rule_url_does_not_assume_other_fields_are_identical() -> None:
    batch = batches(
        [
            v1_row(key_field="a", alert_rule_url="https://g/d/1", severity="error"),
            v1_row(key_field="b", alert_rule_url="https://g/d/1", severity="major"),
        ]
    )[0]
    assert "severity" not in build_request(batch, "1.0.0", PROMPT_VERSION)["shared_fields"]


def test_a_field_present_in_only_some_documents_is_never_shared() -> None:
    assert shared_field_names([{"a": 1, "b": 2}, {"a": 1}]) == {"a"}


def test_factoring_is_lossless_header_plus_row_rebuilds_the_exact_document() -> None:
    alerts = [
        v1_row(key_field="a", alert_rule_url="https://g/d/1", node_name="n1"),
        v2_row(key_field="b", alert_rule_url="https://g/d/1", impact=None, site="dc-1"),
    ]
    for batch in batches(alerts):
        request = build_request(batch, "1.0.0", PROMPT_VERSION)
        assert_lossless(request, batch)
        for index, alert in enumerate(request["alerts"]):
            assert reconstruct_document(request, alert) == batch.alerts[index].source


def test_schema_always_stays_on_the_alert_envelope() -> None:
    request = build_request(batches([v1_row(key_field="a")])[0], "1.0.0", PROMPT_VERSION)
    assert request["alerts"][0]["schema"] == "v1"
    assert "schema" not in request["shared_fields"]


def test_the_serialized_payload_is_stable_for_identical_input() -> None:
    batch = batches([v1_row(key_field="a")])[0]
    first = serialize_request(build_request(batch, "1.0.0", PROMPT_VERSION))
    second = serialize_request(build_request(batch, "1.0.0", PROMPT_VERSION))
    assert first == second


# ------------------------------------------------------- response validation

REQUEST_BATCH_ID = "b1"
REQUEST_ALERT_IDS = ["a1", "a2"]


def good_verdict(alert_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "alert_id": alert_id,
        "assessment": "no_violation",
        "principle_id": "NONE",
        "confidence": "high",
        "justification": "ok",
        **overrides,
    }


def response(batch_id: str, verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    return {"batch_id": batch_id, "verdicts": verdicts}


def validate(body: Any) -> list[Any]:
    return validate_response(body, REQUEST_BATCH_ID, REQUEST_ALERT_IDS)


def test_a_valid_response_returns_verdicts_in_request_order() -> None:
    verdicts = validate(response("b1", [good_verdict("a2"), good_verdict("a1")]))
    assert [v.alert_id for v in verdicts] == ["a1", "a2"]


def test_a_mismatched_batch_id_rejects_the_whole_response() -> None:
    with pytest.raises(LlmResponseError, match="batch_id does not match"):
        validate(response("other", [good_verdict("a1"), good_verdict("a2")]))


def test_a_missing_verdict_rejects_the_whole_response() -> None:
    with pytest.raises(LlmResponseError, match="missing a verdict"):
        validate(response("b1", [good_verdict("a1")]))


def test_an_extra_verdict_for_an_unknown_alert_rejects_the_whole_response() -> None:
    with pytest.raises(LlmResponseError, match="unknown alert"):
        validate(response("b1", [good_verdict("a1"), good_verdict("a2"), good_verdict("a3")]))


def test_a_duplicated_alert_id_rejects_the_whole_response() -> None:
    with pytest.raises(LlmResponseError, match="duplicate verdict"):
        validate(response("b1", [good_verdict("a1"), good_verdict("a1")]))


def test_an_unexpected_field_anywhere_rejects_the_whole_response() -> None:
    with pytest.raises(LlmResponseError, match="unexpected top-level field"):
        validate({"batch_id": "b1", "verdicts": [good_verdict("a1")], "extra": 1})
    with pytest.raises(LlmResponseError, match='unexpected field "score"'):
        validate(response("b1", [good_verdict("a1", score=0.9), good_verdict("a2")]))


@pytest.mark.parametrize(
    ("patch", "pattern"),
    [
        ({"assessment": "maybe"}, "assessment .* is not valid"),
        ({"confidence": 0.9}, "confidence .* is not valid"),
    ],
)
def test_invalid_enum_values_are_rejected(patch: dict[str, Any], pattern: str) -> None:
    with pytest.raises(LlmResponseError, match=pattern):
        validate(response("b1", [good_verdict("a1", **patch), good_verdict("a2")]))


@pytest.mark.parametrize(
    ("patch", "pattern"),
    [
        (
            {"assessment": "no_violation", "principle_id": "P1"},
            "no_violation must carry principle_id NONE",
        ),
        ({"assessment": "other", "principle_id": "P1"}, "other must carry principle_id OTHER"),
        ({"assessment": "catalog_violation", "principle_id": "NONE"}, "not in the catalogue"),
        ({"assessment": "catalog_violation", "principle_id": "P99"}, "not in the catalogue"),
    ],
)
def test_the_assessment_principle_pairing_is_closed(patch: dict[str, Any], pattern: str) -> None:
    with pytest.raises(LlmResponseError, match=pattern):
        validate(response("b1", [good_verdict("a1", **patch), good_verdict("a2")]))


def test_the_model_may_cite_an_r_id_for_something_the_rules_missed() -> None:
    verdicts = validate(
        response(
            "b1",
            [
                good_verdict("a1", assessment="catalog_violation", principle_id="R2"),
                good_verdict("a2"),
            ],
        )
    )
    assert verdicts[0].principle_id == "R2"


@pytest.mark.parametrize(
    ("justification", "pattern"),
    [("   ", "justification is empty"), ("x" * 1001, "exceeds 1000 characters")],
)
def test_an_empty_or_over_long_justification_rejects_the_response(
    justification: str, pattern: str
) -> None:
    with pytest.raises(LlmResponseError, match=pattern):
        validate(
            response("b1", [good_verdict("a1", justification=justification), good_verdict("a2")])
        )


@pytest.mark.parametrize("body", ["nope", [], None, 42])
def test_a_non_object_response_is_rejected(body: Any) -> None:
    with pytest.raises(LlmResponseError):
        validate(body)


def test_only_a_high_confidence_catalogue_violation_is_flagged() -> None:
    def verdict(**overrides: Any) -> dict[str, Any]:
        return good_verdict("a", **overrides)

    assert (
        state_for_verdict(
            verdict(assessment="catalog_violation", principle_id="P1", confidence="high")
        )
        == "llm_flagged"
    )
    for confidence in ("medium", "low"):
        assert (
            state_for_verdict(
                verdict(assessment="catalog_violation", principle_id="P1", confidence=confidence)
            )
            == "needs_review"
        )
    assert state_for_verdict(verdict(assessment="other", principle_id="OTHER")) == "needs_review", (
        "other never counts toward flagged"
    )
    assert state_for_verdict(verdict()) == "assessed_good"


def test_the_structured_output_schema_is_closed_at_both_levels() -> None:
    schema = response_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["verdicts"]["items"]["additionalProperties"] is False


# ---------------------------------------------------------------- assessment


def assess(alerts: list[Any], client: Any | None = None, **kwargs: Any) -> Any:
    return assess_alerts(
        alerts=alerts,
        client=client if client is not None else FakeLlmClient(),
        system_prompt="system",
        run_id=RUN_ID,
        prompt_version=PROMPT_VERSION,
        model_version=MODEL_VERSION,
        now=NOW,
        **kwargs,
    )


def test_every_eligible_alert_receives_exactly_one_outcome() -> None:
    result = assess([v1_row(key_field="a"), v1_row(key_field="b")])
    assert len(result.outcomes) == 2
    assert all(o.state == "assessed_good" for o in result.outcomes.values())


def test_a_durable_verdict_is_reused_instead_of_being_requested_again() -> None:
    alert = v1_row(key_field="a")
    client = FakeLlmClient()
    existing = {
        verdict_key(alert.application, alert.key_field): {
            "assessment": "catalog_violation",
            "principle_id": "P2",
            "confidence": "high",
            "justification": "stored earlier",
        }
    }
    result = assess([alert], client, existing_verdicts=existing)

    assert client.calls == [], "no request should be made for a stored verdict"
    assert result.reused_verdicts == 1
    assert result.new_verdicts == []
    outcome = result.outcomes[alert.identity]
    assert outcome.state == "llm_flagged"
    assert outcome.reused is True


def test_a_successful_batch_produces_durable_verdict_rows_with_the_judged_document() -> None:
    alert = v1_row(key_field="a")
    result = assess([alert])
    assert len(result.new_verdicts) == 1
    verdict = result.new_verdicts[0]
    assert verdict["application"] == alert.application
    assert verdict["doc_hash"] == alert.doc_hash
    assert json.loads(verdict["representative_doc"]) == alert.source
    assert verdict["prompt_version"] == PROMPT_VERSION
    assert verdict["model_version"] == MODEL_VERSION


def test_a_failed_attempt_retries_the_identical_batch_byte_for_byte() -> None:
    alerts = [v1_row(key_field="a"), v1_row(key_field="b")]
    client = FakeLlmClient()
    batch_id = batches(alerts)[0].batch_id
    client.on(batch_id, 1, ScriptedResult("transport_error"))
    client.on(batch_id, 2, ScriptedResult("invalid_json"))

    result = assess(alerts, client)

    payloads = client.payloads_for(batch_id)
    assert len(payloads) == 3
    assert len(set(payloads)) == 1, "every attempt must send identical bytes"
    assert [a["status"] for a in result.batch_attempts] == [
        "transport_error",
        "invalid_response",
        "succeeded",
    ]
    assert len({a["request_hash"] for a in result.batch_attempts}) == 1


def test_a_batch_stops_after_exactly_three_attempts_and_never_a_fourth() -> None:
    client = FakeLlmClient(fallback=ScriptedResult("transport_error"))
    result = assess([v1_row(key_field="a")], client)
    assert len(client.calls) == MAX_ATTEMPTS == 3
    assert len(result.batch_attempts) == 3


def test_an_exhausted_batch_marks_every_member_unassessed_with_the_shared_reason() -> None:
    alerts = [v1_row(key_field="a"), v1_row(key_field="b"), v1_row(key_field="c")]
    result = assess(alerts, FakeLlmClient(fallback=ScriptedResult("timeout")))

    assert len(result.outcomes) == 3
    reasons = {o.unassessed_reason for o in result.outcomes.values()}
    assert all(o.state == "unassessed" for o in result.outcomes.values())
    assert len(reasons) == 1, "the whole batch shares one failure reason"
    assert result.new_verdicts == [], "a failed batch stores no verdicts"


def test_an_invalid_response_is_never_partially_accepted() -> None:
    alerts = [v1_row(key_field="a"), v1_row(key_field="b")]
    batch_id = batches(alerts)[0].batch_id
    # A response that answers only one of the two alerts.
    client = FakeLlmClient(
        fallback=ScriptedResult(
            "raw",
            text=json.dumps({"batch_id": batch_id, "verdicts": [good_verdict("whatever")]}),
        )
    )
    result = assess(alerts, client)
    assert all(o.state == "unassessed" for o in result.outcomes.values())
    assert result.new_verdicts == []


def test_a_batch_that_recovers_on_the_third_attempt_is_assessed_normally() -> None:
    alerts = [v1_row(key_field="a")]
    client = FakeLlmClient()
    batch_id = batches(alerts)[0].batch_id
    client.on(batch_id, 1, ScriptedResult("timeout"))
    client.on(batch_id, 2, ScriptedResult("transport_error"))

    result = assess(alerts, client)
    assert next(iter(result.outcomes.values())).state == "assessed_good"
    assert len(result.new_verdicts) == 1


def test_one_failing_batch_does_not_affect_another_batch_in_the_same_run() -> None:
    alerts = [
        v1_row(key_field="a", alert_rule_url="https://g/d/1"),
        v1_row(key_field="b", alert_rule_url="https://g/d/2"),
    ]
    built = batches(alerts)
    client = FakeLlmClient()
    for attempt in (1, 2, 3):
        client.on(built[0].batch_id, attempt, ScriptedResult("transport_error"))

    result = assess(alerts, client)
    assert sorted(o.state for o in result.outcomes.values()) == ["assessed_good", "unassessed"]


def test_verdict_states_map_through_from_the_model_response() -> None:
    alerts = [
        v1_row(key_field="a", alert_rule_url="https://g/d/1"),
        v1_row(key_field="b", alert_rule_url="https://g/d/1"),
        v1_row(key_field="c", alert_rule_url="https://g/d/1"),
    ]
    decided = [
        {
            "assessment": "catalog_violation",
            "principle_id": "P1",
            "confidence": "high",
            "justification": "j",
        },
        {
            "assessment": "catalog_violation",
            "principle_id": "P1",
            "confidence": "low",
            "justification": "j",
        },
        {
            "assessment": "no_violation",
            "principle_id": "NONE",
            "confidence": "high",
            "justification": "j",
        },
    ]
    client = FakeLlmClient()
    client.on(
        batches(alerts)[0].batch_id,
        1,
        scripted_verdicts(lambda _alert, index, _request: decided[index]),
    )

    result = assess(alerts, client)
    by_key = {o.alert.key_field: o.state for o in result.outcomes.values()}
    assert by_key == {"a": "llm_flagged", "b": "needs_review", "c": "assessed_good"}


def test_running_with_the_model_off_marks_identities_unassessed_with_a_reason() -> None:
    outcomes = mark_all_unassessed([v1_row(key_field="a")], "llm disabled for this run")
    outcome = next(iter(outcomes.values()))
    assert outcome.state == "unassessed"
    assert outcome.unassessed_reason == "llm disabled for this run"


# -------------------------------------------------------------------- prompt


def test_the_prompt_carries_both_guides_verbatim_and_the_full_catalogue() -> None:
    built = build_prompt()
    assert "BEGIN Alerting_Guide_Appchi_EN.md" in built.system_prompt
    assert "BEGIN what_is_an_incorrect_alert_EN.md" in built.system_prompt
    assert "The Wake-Up Test" in built.system_prompt
    assert "that's a log" in built.system_prompt.lower()
    for identifier in ("P1", "P7", "P11", "R1", "R10"):
        assert identifier in built.system_prompt
    assert built.prompt_version == CURRENT_PROMPT_VERSION
    assert len(built.system_prompt_hash) == 64


def test_the_prompt_states_the_numeric_severity_scale() -> None:
    """Severity reaches the model as a number, so the scale has to be in the prompt.

    Without it the guides' severity reasoning - principle P8 above all - has nothing to
    work with, because a bare 5 names no level.
    """
    prompt = build_prompt().system_prompt
    assert "SEVERITY SCALE" in prompt
    assert "`severity` is a NUMBER, not a word" in prompt
    for level in ("error", "critical", "major", "high", "warning", "clear"):
        assert level in prompt
    # The number alone is ambiguous, so the model is told to read it against the schema.
    assert "schema" in prompt[prompt.index("SEVERITY SCALE") :]


def test_the_prompt_instructs_independent_per_alert_assessment() -> None:
    prompt = build_prompt().system_prompt
    assert "Assess every alert INDEPENDENTLY" in prompt
    assert "Never copy a\nneighbour's verdict" in prompt
    assert 'Default to "no_violation"' in prompt
