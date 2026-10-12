"""The ``LlmClient`` contract.

One method, deliberately: the pipeline owns the three-attempt policy, the batch identity,
the validation and the persistence, and a client that could retry or reorder on its own
would make those guarantees untrue.

A client returns raw response text, optionally with endpoint metadata. Parsing and
validation happen in the pipeline so the real adapter and fake use identical standards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = ["Completion", "LlmClient", "LlmTransportError"]


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class LlmTransportError(Exception):
    """A failed call: transport error, timeout, or an empty message."""

    def __init__(
        self,
        message: str,
        kind: str = "transport",
        *,
        metadata: dict[str, Any] | None = None,
        response_text: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.metadata = metadata or {}
        self.response_text = response_text


class LlmClient(Protocol):
    """What the pipeline needs from any model transport."""

    model_version: str

    def complete(
        self, *, system_prompt: str, request_text: str, batch_id: str, attempt: int
    ) -> str | Completion:
        """Return the raw response text, or raise :class:`LlmTransportError`."""
        ...
