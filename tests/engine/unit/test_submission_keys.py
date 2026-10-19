"""Which stored submission a retried key returns, without a database.

Submission keys are unique across every client (design "Admin console without login",
2026-10-19). Migration 019 kept a key that several people had used as one ``#<id>`` row
each, so a key can match several rows.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from src.engine.api.jobs import matching_submission

CONFLICT = "This submission key was already used with different options."
SAME = "a" * 64
OTHER = "b" * 64


def row(job_id: str, request_hash: str, requested_by: str = "") -> dict[str, Any]:
    return {"job_id": job_id, "request_hash": request_hash, "requested_by": requested_by}


def refusal(rows: list[dict[str, Any]], request_hash: str) -> HTTPException:
    with pytest.raises(HTTPException) as caught:
        matching_submission(rows, request_hash, CONFLICT)
    assert caught.value.status_code == 409
    return caught.value


def test_a_new_key_matches_nothing() -> None:
    assert matching_submission([], SAME, CONFLICT) is None


def test_a_retry_returns_its_own_submission() -> None:
    stored = row("1", SAME)
    assert matching_submission([stored], SAME, CONFLICT) is stored


def test_a_key_reused_with_other_options_is_refused() -> None:
    assert refusal([row("1", OTHER)], SAME).detail == CONFLICT


def test_after_019_a_retry_returns_the_one_migrated_row_with_its_options() -> None:
    mine = row("1", SAME, "#1")
    assert matching_submission([row("2", OTHER, "#2"), mine], SAME, CONFLICT) is mine
    assert refusal([row("1", OTHER, "#1"), row("2", OTHER, "#2")], SAME).detail == CONFLICT


def test_after_019_a_key_two_people_used_with_the_same_options_is_refused() -> None:
    detail = refusal([row("1", SAME, "#1"), row("2", SAME, "#2")], SAME).detail
    assert detail.startswith("Several earlier submissions used this key.")
