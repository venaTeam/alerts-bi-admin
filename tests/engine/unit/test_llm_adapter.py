from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from openai import OpenAI
from src.engine.config.llm import LlmConfig
from src.engine.llm.client import LlmTransportError
from src.engine.llm.openai_client import OpenAiLlmClient

CONFIG = LlmConfig(True, "https://model.invalid/v1", "test", "deployment", 1000, 200, False)


def answer(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "completion-1",
        "object": "chat.completion",
        "created": 1,
        "model": "weights-v1",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        **overrides,
    }


def test_adapter_sends_closed_schema_and_records_usage_without_assuming_cache_hits() -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=answer())

    sdk = OpenAI(
        base_url=CONFIG.base_url,
        api_key="test",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    config = replace(CONFIG, model_revision="weights-v1", max_completion_tokens=4096)
    adapter = OpenAiLlmClient(config, sdk)
    result = adapter.complete(system_prompt="system", request_text="{}", batch_id="b", attempt=1)
    assert result.metadata == {
        "model": "weights-v1",
        "finish_reason": "stop",
        "input_tokens": 100,
        "output_tokens": 20,
        "cached_tokens": None,
    }
    assert adapter.model_version == "weights-v1"
    assert requests[0]["temperature"] == 0
    assert requests[0]["response_format"]["json_schema"]["strict"] is True
    assert requests[0]["max_completion_tokens"] == 4096


@pytest.mark.parametrize(
    ("finish", "refusal", "kind"), [("length", None, "incomplete"), ("stop", "refused", "refusal")]
)
def test_non_completed_responses_are_failures(finish: str, refusal: str | None, kind: str) -> None:
    body = answer(
        choices=[
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": "{}", "refusal": refusal},
            }
        ]
    )
    sdk = OpenAI(
        api_key="test",
        max_retries=0,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=body))
        ),
    )
    with pytest.raises(LlmTransportError) as caught:
        OpenAiLlmClient(CONFIG, sdk).complete(
            system_prompt="s", request_text="{}", batch_id="b", attempt=1
        )
    assert caught.value.kind == kind
    assert caught.value.response_text == "{}"


def test_owned_sdk_disables_hidden_retries() -> None:
    adapter = OpenAiLlmClient(CONFIG)
    assert adapter.client.max_retries == 0
    adapter.client.close()


@pytest.mark.parametrize("failure", ["timeout", "server", "empty", "no_choices"])
def test_failures_consume_one_sdk_request_without_leaking_response_text(failure: str) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private alert text", request=request)
        if failure == "server":
            return httpx.Response(500, json={"error": {"message": "private alert text"}})
        body = (
            answer(choices=[])
            if failure == "no_choices"
            else answer(
                choices=[
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": ""},
                    }
                ]
            )
        )
        return httpx.Response(200, json=body)

    with (
        OpenAI(
            api_key="test",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        ) as sdk,
        pytest.raises(LlmTransportError) as caught,
    ):
        OpenAiLlmClient(CONFIG, sdk).complete(
            system_prompt="s", request_text="{}", batch_id="b", attempt=1
        )
    assert len(requests) == 1
    assert "private alert text" not in str(caught.value)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after": "7"}, 7.0),
        ({"retry-after": "1.5"}, 1.5),
        ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),
        ({"retry-after": "-3"}, None),
        ({"retry-after": "nan"}, None),
        ({}, None),
    ],
)
def test_a_rate_limit_carries_the_endpoints_retry_after(
    headers: dict[str, str], expected: float | None
) -> None:
    with (
        OpenAI(
            api_key="test",
            max_retries=0,
            http_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _r: httpx.Response(429, headers=headers, json={"error": {}})
                )
            ),
        ) as sdk,
        pytest.raises(LlmTransportError) as caught,
    ):
        OpenAiLlmClient(CONFIG, sdk).complete(
            system_prompt="s", request_text="{}", batch_id="b", attempt=1
        )
    assert caught.value.kind == "transport"
    assert caught.value.retry_after == expected
