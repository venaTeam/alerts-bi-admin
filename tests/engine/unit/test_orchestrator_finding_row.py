"""The R6 episode facts reach the stored finding row, with naive UTC datetimes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from src.engine.domain.normalize import AlertRecord
from src.engine.rules.engine import evaluate_rows
from src.engine.run.orchestrator import _build_finding_row

from tests.engine.helpers.rows import v1_row

WINDOW_END = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)


def _row(at: datetime, severity: int = 5) -> AlertRecord:
    return v1_row(**{"@timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "severity": severity})


def _stored(rows: list[AlertRecord]) -> dict[str, Any]:
    (identity,) = evaluate_rows(rows, WINDOW_END).identities.values()
    return _build_finding_row("r" * 64, identity, None, None)


def test_open_episode_facts_are_stored_with_a_naive_utc_open_since() -> None:
    opened = WINDOW_END - timedelta(hours=80)
    row = _stored([_row(opened), _row(opened + timedelta(hours=72))])
    assert row["max_episode_firing_rows"] == 2
    assert row["open_since"] == opened.replace(tzinfo=None)
    assert row["open_since"].tzinfo is None
    assert row["fire_pattern"] == "stuck"


def test_a_cleared_identity_stores_no_open_since() -> None:
    start = WINDOW_END - timedelta(hours=10)
    row = _stored([_row(start), _row(start + timedelta(hours=1), severity=1)])
    assert row["open_since"] is None
    assert row["max_episode_firing_rows"] == 1
