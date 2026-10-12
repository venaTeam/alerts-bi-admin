"""Exact R6 facts from chronological rows, without retaining event documents."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from alerts_bi_shared.catalogs import (
    R6_API_MIN_SPAN,
    R6_API_RATE_WINDOW,
    R6_API_SPAM_PER_24H,
    R6_FLAP_CYCLES,
    R6_FLAP_WINDOW,
    R6_STUCK_OPEN,
)

from ..domain.normalize import AlertRecord
from .firing import FiringFacts, is_clear


@dataclass(slots=True)
class FiringAccumulator:
    first: datetime | None = None
    pending_at: datetime | None = None
    pending_fires: int = 0
    pending_clears: int = 0
    count: int = 0
    clear_count: int = 0
    current: int = 0
    max_episode: int = 0
    open_since: datetime | None = None
    cycles: deque[datetime] = field(default_factory=deque)
    max_cycles: int = 0

    def add(self, row: AlertRecord) -> None:
        at = row.timestamp
        if self.pending_at is not None:
            if at < self.pending_at:
                raise ValueError("firing analysis requires chronological input")
            if at != self.pending_at:
                self._flush()
        if self.first is None:
            self.first = at
        self.pending_at = at
        self.count += 1
        if is_clear(row):
            self.pending_clears += 1
            self.clear_count += 1
        else:
            self.pending_fires += 1

    def _flush(self) -> None:
        at = self.pending_at
        if at is None:
            return
        # Across page boundaries too: fires at an instant precede all clears at that
        # instant. Hash order within either class cannot change any episode fact.
        if self.pending_fires:
            if self.current == 0:
                self.open_since = at
            self.current += self.pending_fires
            self.max_episode = max(self.max_episode, self.current)
        while self.cycles and at - self.cycles[0] >= R6_FLAP_WINDOW:
            self.cycles.popleft()
        if self.pending_clears:
            if self.current:
                self.cycles.append(at)
                self.max_cycles = max(self.max_cycles, len(self.cycles))
            self.current = 0
            self.open_since = None
        self.pending_fires = self.pending_clears = 0

    def finish(self, provider: str | None) -> FiringFacts:
        self._flush()
        if self.first is None or self.pending_at is None:
            raise ValueError("cannot finish empty firing history")
        span = self.pending_at - self.first
        open_span = None if self.open_since is None else self.pending_at - self.open_since
        rate = self.count / (span / R6_API_RATE_WINDOW) if span >= R6_API_MIN_SPAN else None
        pattern = None
        if self.max_cycles >= R6_FLAP_CYCLES:
            pattern = "flapping"
        elif (
            provider != "grafana"
            and span >= R6_API_MIN_SPAN
            and self.count * R6_API_RATE_WINDOW >= R6_API_SPAM_PER_24H * span
        ):
            pattern = "spamming"
        elif provider == "grafana" and open_span is not None and open_span >= R6_STUCK_OPEN:
            pattern = "stuck"
        self.cycles.clear()
        return FiringFacts(
            self.clear_count,
            self.max_cycles,
            self.max_episode,
            self.open_since,
            open_span,
            span,
            rate,
            pattern,
        )
