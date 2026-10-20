"""Every model group is logged once, with its alerts (design "Log shipping to Elasticsearch")."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from alerts_bi_shared import logging_setup
from alerts_bi_shared.logging_setup import configure_logging, log_context
from alerts_bi_shared.versions import PROMPT_VERSION
from src.engine.llm.assess import assess_alerts
from src.engine.llm.fake import FakeLlmClient, ScriptedResult

from tests.engine.helpers.rows import v1_row

RUN_ID = "run-1"


@pytest.fixture(autouse=True)
def json_at_debug(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(logging_setup, "_settings", None)
    configure_logging("worker", level="debug", fmt="json", capture_libraries=False)
    yield


def _groups(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    return [record for record in records if record["event"] == "llm.group"]


def _assess(alerts: list[Any], client: FakeLlmClient) -> None:
    with log_context(team_id="t1", run_id=RUN_ID):
        assess_alerts(
            alerts=alerts,
            client=client,
            system_prompt="system",
            run_id=RUN_ID,
            prompt_version=PROMPT_VERSION,
            model_version="fake",
            now=datetime(2026, 8, 25, tzinfo=UTC),
        )


def test_a_group_record_lists_each_alert_with_its_outcome(
    capsys: pytest.CaptureFixture[str],
) -> None:
    alerts = [v1_row(key_field="disk-full"), v1_row(key_field="cpu-high")]
    _assess(alerts, FakeLlmClient())

    (group,) = _groups(capsys)
    assert group["stream"] == "runs" and group["run_id"] == RUN_ID and group["batch_id"]
    fields = group["data"]["llm_group"]
    assert (fields["status"], fields["alert_count"], fields["attempts"]) == ("succeeded", 2, 1)
    assert {(a["application"], a["key_field"]) for a in group["alerts"]} == {
        (alerts[0].application, "disk-full"),
        (alerts[1].application, "cpu-high"),
    }
    for alert in group["alerts"]:
        assert set(alert) == {
            "schema",
            "application",
            "key_field",
            "alert_id",
            "state",
            "assessment",
            "principle_id",
            "confidence",
        }, "identity and outcome only: no document, no justification"
        assert alert["state"] == "assessed_good"
    assert "2 alerts assessed on attempt 1" in group["message"]


def test_an_exhausted_group_lists_its_alerts_as_unassessed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    alerts = [v1_row(key_field="a"), v1_row(key_field="b")]
    _assess(alerts, FakeLlmClient(fallback=ScriptedResult("timeout")))

    (group,) = _groups(capsys)
    assert group["data"]["llm_group"]["status"] == "exhausted"
    assert group["data"]["llm_group"]["attempts"] == 3
    assert [alert["state"] for alert in group["alerts"]] == ["unassessed", "unassessed"]
    assert "failed all 3 attempts" in group["message"]
