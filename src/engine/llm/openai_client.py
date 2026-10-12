"""On-prem OpenAI-compatible adapter (design section 5.1; flow step 6.3).

The regular OpenAI Python SDK against the on-prem base URL, using Chat Completions with
strict JSON-schema output and temperature zero.

SDK automatic retries are disabled (``max_retries=0``). This is not a preference: the
pipeline owns a three-attempt policy and records every attempt, and hidden SDK retries
would silently exceed it and break the audit trail. A timeout or transport error consumes
one recorded attempt like any other failed call.
"""

from __future__ import annotations

from typing import Any

from openai import APITimeoutError, OpenAI, OpenAIError

from ..config import LlmConfig
from .client import Completion, LlmTransportError
from .response import response_json_schema

__all__ = ["OpenAiLlmClient"]


class OpenAiLlmClient:
    requires_audit = True

    def __init__(self, config: LlmConfig, client: Any | None = None) -> None:
        if not config.base_url:
            raise ValueError("LLM_BASE_URL is required to call the model")
        if not config.model:
            raise ValueError("LLM_MODEL is required to call the model")
        self.config = config
        self.model_version = config.model_revision or config.model
        self.client = client or OpenAI(
            base_url=config.base_url,
            api_key=config.api_key or "not-used",
            timeout=config.timeout_ms / 1000,
            max_retries=0,
        )

    def complete(
        self, *, system_prompt: str, request_text: str, batch_id: str, attempt: int
    ) -> Completion:
        options: dict[str, Any] = {}
        if self.config.max_completion_tokens is not None:
            options["max_completion_tokens"] = self.config.max_completion_tokens
        try:
            completion = self.client.chat.completions.create(
                model=self.config.model,
                temperature=0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": request_text},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "alert_batch_verdicts",
                        "strict": True,
                        "schema": response_json_schema(),
                    },
                },
                **options,
            )
        except APITimeoutError as exc:
            raise LlmTransportError("model request timed out", "timeout") from exc
        except OpenAIError as exc:
            raise LlmTransportError(
                f"model request failed: {type(exc).__name__}", "transport"
            ) from exc

        choice = completion.choices[0] if completion.choices else None
        usage = completion.usage
        details = usage.prompt_tokens_details if usage is not None else None
        metadata = {
            "model": completion.model,
            "finish_reason": choice.finish_reason if choice is not None else None,
            "input_tokens": usage.prompt_tokens if usage is not None else None,
            "output_tokens": usage.completion_tokens if usage is not None else None,
            "cached_tokens": details.cached_tokens if details is not None else None,
        }
        content = choice.message.content if choice is not None else None
        if choice is not None and choice.message.refusal:
            raise LlmTransportError(
                "model refused the request", "refusal", metadata=metadata, response_text=content
            )
        if choice is not None and choice.finish_reason != "stop":
            raise LlmTransportError(
                "model did not complete the response",
                "incomplete",
                metadata=metadata,
                response_text=content,
            )
        if not isinstance(content, str) or content.strip() == "":
            raise LlmTransportError("model returned an empty message", "empty", metadata=metadata)
        return Completion(content, metadata)
