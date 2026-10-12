"""R6: one alert's firing episodes (design 7.14).

Grafana writes one Elasticsearch row on every evaluation and its repeat interval is
disabled, so a still-firing Grafana alert keeps producing rows. A Grafana row count therefore
reflects evaluation frequency, and R6 never judges row counts for Grafana alerts. Episodes,
meaning clear transitions, carry the meaning.

An *episode* is a maximal run of consecutive firing (non-clear) rows, ordered by
``(timestamp, is_clear, doc_hash)``; a clear row closes it, and a clear sharing an instant
with a firing row sorts after it. The firing rows after the last clear form the *open
episode*.

Stuck measures the open episode as its rows show it: from ``open_since`` to the last firing
row, never to the window's end. Grafana writes a row on every evaluation while an alert
fires, so silence after the last row means the alert is no longer firing (it resolved, or
the rule was deleted or paused), not that it is stuck. One row spans zero and is never
stuck. An alert that started firing before the window still writes rows throughout it, so
its open episode starts at its first in-window row and stuck reads "the open episode's
firing rows span at least 72h of the window".

Spamming applies to non-Grafana (API) alerts only; stuck to Grafana alerts only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from alerts_bi_shared.catalogs import (
    R6_API_MIN_SPAN,
    R6_API_RATE_WINDOW,
    R6_API_SPAM_PER_24H,
    R6_FLAP_CYCLES,
    R6_FLAP_WINDOW,
    R6_STUCK_OPEN,
)

from ..domain.normalize import AlertRecord

__all__ = ["FiringFacts", "firing_facts", "is_clear"]


@dataclass(frozen=True, slots=True)
class FiringFacts:
    clear_count: int
    max_clear_cycles_24h: int
    max_episode_firing_rows: int
    open_since: datetime | None
    """First firing row of the open episode, when the last row is firing."""
    open_span: timedelta | None
    """Last firing row minus ``open_since``: how long the open episode's firing rows span.
    None when no episode is open; zero for an open episode of one row."""
    span: timedelta
    """``last - first``; zero for one row."""
    events_per_24h: float | None
    """Display only; None when the span is under the API minimum."""
    pattern: str | None


def is_clear(row: AlertRecord) -> bool:
    """v1 clears with severity ``clear`` (code 1); v2 with ``status = resolved``."""
    if row.schema == "v1":
        return row.severity == "clear"
    return row.status == "resolved"


def firing_facts(schema: str, rows: Sequence[AlertRecord], provider: str | None) -> FiringFacts:
    """Facts and R6 pattern for one identity's rows (``rows`` must not be empty).

    ``provider`` is the representative row's provider. Nothing here reads the window's end:
    every fact, stuck included, comes from the rows alone. Thresholds use exact
    ``timedelta`` arithmetic.
    """
    # Rows at the same instant order firing before clear, then by document hash: a clear
    # sharing an instant with a firing row closes the episode. That is the conservative
    # reading, because it never invents "stuck".
    ordered = sorted(rows, key=lambda r: (r.timestamp, is_clear(r), r.doc_hash))
    clears = [is_clear(r) for r in ordered]
    cycle_times = [
        ordered[i].timestamp for i in range(1, len(ordered)) if clears[i] and not clears[i - 1]
    ]
    max_cycles = 0
    start = 0
    for end, t in enumerate(cycle_times):
        while t - cycle_times[start] >= R6_FLAP_WINDOW:
            start += 1
        max_cycles = max(max_cycles, end - start + 1)

    max_episode = 0
    current = 0
    episode_start: datetime | None = None
    for row, clear in zip(ordered, clears, strict=True):
        if clear:
            current = 0
            episode_start = None
            continue
        if current == 0:
            episode_start = row.timestamp
        current += 1
        max_episode = max(max_episode, current)
    open_since = episode_start if current > 0 else None
    # When an episode is open the last ordered row is firing, so it is the last firing row.
    open_span = None if open_since is None else ordered[-1].timestamp - open_since

    n = len(ordered)
    span = ordered[-1].timestamp - ordered[0].timestamp
    grafana = provider == "grafana"
    events_per_24h = n / (span / R6_API_RATE_WINDOW) if span >= R6_API_MIN_SPAN else None
    clear_count = sum(clears)

    pattern: str | None = None
    if max_cycles >= R6_FLAP_CYCLES:
        pattern = "flapping"
    elif (
        not grafana
        and span >= R6_API_MIN_SPAN
        and n * R6_API_RATE_WINDOW >= R6_API_SPAM_PER_24H * span
    ):
        pattern = "spamming"
    elif grafana and open_span is not None and open_span >= R6_STUCK_OPEN:
        pattern = "stuck"
    return FiringFacts(
        clear_count, max_cycles, max_episode, open_since, open_span, span, events_per_24h, pattern
    )
