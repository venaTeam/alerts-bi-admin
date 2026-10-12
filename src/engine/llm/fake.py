"""Deterministic ``LlmClient`` fake (design section 5.1, decided 2026-08-29).

Tests use this, never the network. Scripted results are selected by (batch_id, attempt
number), which is what makes "the second attempt succeeds" and "all three attempts fail"
expressible without timing or randomness.

It also records every call, so a test can assert the two properties that matter most:
exactly three attempts, and byte-identical retry payloads.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .client import LlmTransportError

__all__ = ["FakeLlmClient", "ScriptedResult", "scripted_verdicts"]


@dataclass(frozen=True, slots=True)
class ScriptedResult:
    kind: str
    """``ok`` | ``raw`` | ``transport_error`` | ``timeout`` | ``invalid_json``"""
    text: str | None = None
    """Raw response text for ``raw`` and ``invalid_json``."""
    build: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    """Response builder for ``ok``."""


@dataclass(slots=True)
class RecordedCall:
    batch_id: str
    attempt: int
    request_text: str
    system_prompt: str


class FakeLlmClient:
    def __init__(
        self,
        model_version: str = "fake-model-1",
        script: dict[tuple[str, int], ScriptedResult] | None = None,
        fallback: ScriptedResult | None = None,
    ) -> None:
        self.model_version = model_version
        self.script = script or {}
        self.fallback = fallback or ScriptedResult("ok")
        self.calls: list[RecordedCall] = []

    def on(self, batch_id: str, attempt: int, result: ScriptedResult) -> FakeLlmClient:
        """Script a result for one batch attempt."""
        self.script[(batch_id, attempt)] = result
        return self

    def payloads_for(self, batch_id: str) -> list[str]:
        """Every request text seen for a batch, in attempt order.

        A test asserts these are identical to prove retries resend the same bytes.
        """
        return [call.request_text for call in self.calls if call.batch_id == batch_id]

    def complete(
        self, *, system_prompt: str, request_text: str, batch_id: str, attempt: int
    ) -> str:
        self.calls.append(RecordedCall(batch_id, attempt, request_text, system_prompt))

        result = self.script.get((batch_id, attempt), self.fallback)

        if result.kind == "transport_error":
            raise LlmTransportError(result.text or "scripted transport failure", "transport")
        if result.kind == "timeout":
            raise LlmTransportError(result.text or "scripted timeout", "timeout")
        if result.kind == "invalid_json":
            return result.text or "{not valid json"
        if result.kind == "raw":
            return result.text or ""
        return json.dumps(self._build_default(json.loads(request_text), result))

    @staticmethod
    def _build_default(request: dict[str, Any], result: ScriptedResult) -> dict[str, Any]:
        """Default success response: every alert assessed, no violation."""
        if result.build is not None:
            return result.build(request)
        return {
            "batch_id": request["batch_id"],
            "verdicts": [
                {
                    "alert_id": alert["alert_id"],
                    "assessment": "no_violation",
                    "principle_id": "NONE",
                    "confidence": "high",
                    "justification": "Deterministic fake: no violation.",
                }
                for alert in request["alerts"]
            ],
        }


def scripted_verdicts(
    decide: Callable[[dict[str, Any], int, dict[str, Any]], dict[str, Any]],
) -> ScriptedResult:
    """Build a scripted success whose verdicts are chosen per alert."""

    def build(request: dict[str, Any]) -> dict[str, Any]:
        return {
            "batch_id": request["batch_id"],
            "verdicts": [
                {"alert_id": alert["alert_id"], **decide(alert, index, request)}
                for index, alert in enumerate(request["alerts"])
            ],
        }

    return ScriptedResult("ok", build=build)
