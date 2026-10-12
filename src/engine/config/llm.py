"""On-prem model settings.

The batch ceiling is validated here rather than at the call site: 200 is a design decision
(section 5.1), and a configuration that violates it should fail at load, before a run has
read a single alert.
"""

from __future__ import annotations

from dataclasses import dataclass

from alerts_bi_shared.config.env import read_bool, read_int, read_str

__all__ = ["MAX_BATCH_SIZE_CEILING", "LlmConfig", "load_llm_config"]

#: Hard ceiling from design section 5.1. Configuration may lower it, never raise it.
MAX_BATCH_SIZE_CEILING = 200


@dataclass(frozen=True, slots=True)
class LlmConfig:
    enabled: bool
    base_url: str
    api_key: str
    model: str
    timeout_ms: int
    max_batch_size: int
    live_test: bool
    model_revision: str = ""
    max_completion_tokens: int | None = None


def load_llm_config() -> LlmConfig:
    max_batch_size = read_int("LLM_MAX_BATCH_SIZE", MAX_BATCH_SIZE_CEILING)
    if max_batch_size < 1:
        raise ValueError("LLM_MAX_BATCH_SIZE must be a positive integer")
    if max_batch_size > MAX_BATCH_SIZE_CEILING:
        # Section 5.1 fixes 200 as a hard count ceiling. Section 7.2 allows lowering it
        # after capacity measurement, never raising it.
        raise ValueError(
            f"LLM_MAX_BATCH_SIZE {max_batch_size} exceeds the design ceiling "
            f"of {MAX_BATCH_SIZE_CEILING}"
        )

    token_limit = read_int("LLM_MAX_COMPLETION_TOKENS", 0)
    if token_limit < 0:
        raise ValueError("LLM_MAX_COMPLETION_TOKENS must be nonnegative")
    revision = read_str("LLM_MODEL_REVISION")
    if len(revision) > 128:
        raise ValueError("LLM_MODEL_REVISION exceeds 128 characters")
    return LlmConfig(
        enabled=read_bool("LLM_ENABLED", False),
        base_url=read_str("LLM_BASE_URL"),
        api_key=read_str("LLM_API_KEY"),
        model=read_str("LLM_MODEL"),
        timeout_ms=read_int("LLM_TIMEOUT_MS", 120000),
        max_batch_size=max_batch_size,
        live_test=read_bool("LLM_LIVE_TEST", False),
        model_revision=revision,
        max_completion_tokens=token_limit or None,
    )
