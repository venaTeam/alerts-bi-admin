from __future__ import annotations

import pytest
from src.engine.domain.normalize import AlertRecord
from src.engine.llm.assess import assess_alerts
from src.engine.llm.fake import FakeLlmClient, scripted_verdicts
from src.engine.llm.grouping import build_batches
from src.engine.llm.response import LlmResponseError, validate_response

from tests.engine.helpers.rows import v1_row, v2_row
from tests.engine.unit.test_llm import (
    MODEL_VERSION,
    NOW,
    PROMPT_VERSION,
    RUN_ID,
    good_verdict,
    response,
)


@pytest.mark.parametrize(
    ("alert", "principle"),
    [(v1_row(), p) for p in ("P7", "P8", "P9", "R8", "R9", "R10")]
    + [(v2_row(), "R7"), (v1_row(provider="api"), "R4")]
    + [(v2_row(severity=3), "P7"), (v2_row(severity=9), "P7")],
)
def test_inapplicable_citations_reject_the_response(alert: AlertRecord, principle: str) -> None:
    body = response(
        "b", [good_verdict("a", assessment="catalog_violation", principle_id=principle)]
    )
    with pytest.raises(LlmResponseError, match="not applicable"):
        validate_response(body, "b", ["a"], alerts=[alert])


def test_live_client_cannot_call_without_durable_audit() -> None:
    class LiveClient(FakeLlmClient):
        requires_audit = True

    client = LiveClient()
    with pytest.raises(ValueError, match="requires a SQL audit journal"):
        assess_alerts(
            alerts=[v1_row()],
            client=client,
            system_prompt="system",
            run_id=RUN_ID,
            prompt_version=PROMPT_VERSION,
            model_version=MODEL_VERSION,
            now=NOW,
        )
    assert not client.calls


def test_applicability_is_bound_to_id_when_verdicts_are_reordered() -> None:
    body = response(
        "b",
        [good_verdict("v2", assessment="catalog_violation", principle_id="P9"), good_verdict("v1")],
    )
    result = validate_response(body, "b", ["v1", "v2"], alerts=[v1_row(), v2_row()])
    assert [v.principle_id for v in result] == ["NONE", "P9"]


def test_inapplicable_citation_retries_whole_batch_and_never_becomes_good() -> None:
    alerts = [v1_row(key_field="a"), v1_row(key_field="b")]
    client = FakeLlmClient(
        fallback=scripted_verdicts(
            lambda _a, _i, _r: {
                "assessment": "catalog_violation",
                "principle_id": "P9",
                "confidence": "high",
                "justification": "Unsupported v2 finding.",
            }
        )
    )
    result = assess_alerts(
        alerts=alerts,
        client=client,
        system_prompt="system",
        run_id=RUN_ID,
        prompt_version=PROMPT_VERSION,
        model_version=MODEL_VERSION,
        now=NOW,
    )
    assert {o.state for o in result.outcomes.values()} == {"unassessed"}
    batch = build_batches(alerts, RUN_ID, PROMPT_VERSION, MODEL_VERSION)[0]
    assert len(client.payloads_for(batch.batch_id)) == 3
    assert len(set(client.payloads_for(batch.batch_id))) == 1
    assert not result.new_verdicts
