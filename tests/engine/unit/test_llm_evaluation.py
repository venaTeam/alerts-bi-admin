from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from scripts.evaluate_llm import main
from scripts.generate_mock_alerts import review_documents
from src.engine.llm.assess import assess_alerts
from src.engine.llm.evaluation import compare_trials, score_trial, validate_corpus
from src.engine.llm.fake import FakeLlmClient, ScriptedResult, scripted_verdicts

ROOT = Path(__file__).resolve().parents[3]


def manifest() -> dict[str, Any]:
    return json.loads((ROOT / "test/fixtures/llm-review-cases.json").read_bytes())  # type: ignore[no-any-return]


def test_corpus_is_stable_and_split_by_family_and_actual_group() -> None:
    data = manifest()
    documents = review_documents(data["cases"])
    assert documents == review_documents(data["cases"])
    cases = validate_corpus(data, documents)
    assert len(cases) == 14
    bad = copy.deepcopy(data)
    bad["cases"][1]["split"] = "holdout"
    with pytest.raises(ValueError, match="must not cross splits"):
        validate_corpus(bad, documents)


def test_frozen_representatives_are_not_rewritten_by_fixture_defaults() -> None:
    source = {"application": "actual-app", "message": "original", "nested": {"value": [1, 2]}}
    documents = review_documents([{"source": source}])
    assert documents == [source]
    assert documents[0] is not source


def test_live_runner_requires_explicit_opt_in_before_constructing_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_LIVE_TEST", "false")
    with pytest.raises(SystemExit) as caught:
        main(["--mode", "live"])
    assert caught.value.code == 2


def test_runner_stops_after_failed_trial_and_writes_partial_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scripts.evaluate_llm.FakeLlmClient",
        lambda: FakeLlmClient(fallback=ScriptedResult("timeout")),
    )
    output = tmp_path / "summary.json"
    assert main(["--mode", "fake", "--caps", "1", "10", "--out", str(output)]) == 1
    report = json.loads(output.read_bytes())
    assert len(report["trials"]) == 1
    assert report["release_ready"] is False
    assert report["trials"][0]["metrics"]["unassessed"] == 8
    assert "request_payload" not in output.read_text()


def test_fake_all_good_does_not_claim_perfect_precision_and_detects_misses() -> None:
    data = manifest()
    cases = validate_corpus(data, review_documents(data["cases"]))
    result = assess_alerts(
        alerts=[c.alert for c in cases],
        client=FakeLlmClient(),
        system_prompt="evaluation",
        run_id="eval",
        prompt_version="1",
        model_version="fake",
        now=datetime.now(UTC),
    )
    scored = score_trial(cases, result)
    assert scored["metrics"]["high_precision"] is None
    assert scored["metrics"]["missed_as_good"] == 4
    assert scored["usage"]["input_tokens"] is None
    assert compare_trials(scored, scored)["changed_cases"] == []


def test_metrics_weight_groups_separately_and_count_false_highs() -> None:
    data = manifest()
    cases = validate_corpus(data, review_documents(data["cases"]))[:3]
    client = FakeLlmClient(
        fallback=scripted_verdicts(
            lambda _a, _i, _r: {
                "assessment": "catalog_violation",
                "principle_id": "R2",
                "confidence": "high",
                "justification": "scripted catalogue verdict",
            }
        )
    )
    result = assess_alerts(
        alerts=[c.alert for c in cases],
        client=client,
        system_prompt="evaluation",
        run_id="eval",
        prompt_version="1",
        model_version="fake",
        now=datetime.now(UTC),
    )
    score = score_trial(cases, result)
    assert score["metrics"]["high_precision"] == pytest.approx(1 / 3)
    assert score["group_weighted_precision"] == 0.25
    assert score["high_precision_cluster_interval_95"] == [0, 0.5]


def test_unassessed_is_separate_from_good_and_representation_remains_lossless() -> None:
    data = manifest()
    cases = validate_corpus(data, review_documents(data["cases"]))[:2]
    client = FakeLlmClient(fallback=ScriptedResult("invalid_json"))
    result = assess_alerts(
        alerts=[c.alert for c in cases],
        client=client,
        system_prompt="evaluation",
        run_id="eval",
        prompt_version="1",
        model_version="fake",
        now=datetime.now(UTC),
        factored=False,
        reverse_order=True,
    )
    scored = score_trial(cases, result)
    assert scored["metrics"]["unassessed"] == 2
    assert scored["metrics"]["missed_as_good"] == 0
    payloads = [json.loads(call.request_text) for call in client.calls]
    assert len(client.calls) == 3
    assert not payloads[0]["shared_fields"]
    assert {json.dumps(a["fields"], sort_keys=True) for a in payloads[0]["alerts"]} == {
        json.dumps(c.alert.source, sort_keys=True) for c in cases
    }
