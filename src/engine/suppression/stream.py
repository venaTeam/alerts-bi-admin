"""Whole-schema suppression guard with compact predicate signatures, not row sets."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from alerts_bi_operations.registry import Panel

from ..domain.normalize import AlertRecord
from .evaluate import (
    BLAST_RADIUS_LIMIT,
    PanelInterpretation,
    _leaf_excludes_row,
    _leaf_hides_row,
    interpret_panel,
)


@dataclass(slots=True)
class SuppressionSummary:
    unmeasured_leaves: int
    interpretations: list[PanelInterpretation]
    notes: list[str]
    unseen_unmeasured: int


class SuppressionAccumulator:
    def __init__(self, panels: Sequence[Panel]) -> None:
        self.interpretations = [interpret_panel(p) for p in panels]
        self.leaves = [
            leaf for p in self.interpretations for leaf in p.leaves if leaf.kind == "suppression"
        ]
        self.matches = [0] * len(self.leaves)
        self.panel_masks = [
            sum(
                1 << i
                for i, leaf in enumerate(self.leaves)
                if any(leaf is candidate for candidate in p.leaves)
            )
            for p in self.interpretations
        ]
        self.identity_leaves = [
            [leaf for leaf in p.leaves if leaf.kind == "identity"] for p in self.interpretations
        ]
        self.total = 0
        self.active_mask = 0

    def add(self, row: AlertRecord) -> tuple[int, bool]:
        self.total += 1
        mask = 0
        for i, leaf in enumerate(self.leaves):
            if _leaf_excludes_row(leaf, row):
                mask |= 1 << i
                self.matches[i] += 1
        hidden = bool(self.identity_leaves) and all(
            any(_leaf_hides_row(leaf, row) for leaf in leaves) for leaves in self.identity_leaves
        )
        return mask, hidden

    def finish(self) -> SuppressionSummary:
        # Only now is the denominator known. A page-local guard would change R5.
        for i, leaf in enumerate(self.leaves):
            matched = self.matches[i]
            leaf.matched_rows = matched
            if self.total and matched / self.total > BLAST_RADIUS_LIMIT:
                leaf.kind = "unmeasured"
                leaf.reason = (
                    f"blast radius {matched}/{self.total} exceeds "
                    f"{BLAST_RADIUS_LIMIT * 100:.0f}% of owned rows; routed to human review"
                )
            else:
                self.active_mask |= 1 << i
        unmeasured = unseen_unmeasured = 0
        notes: list[str] = []
        for panel in self.interpretations:
            if panel.safety_state == "unparseable":
                unmeasured += 1
                unseen_unmeasured += 1
                notes.append(f"panel {panel.panel_id}: {panel.unmeasured_reason}")
            for leaf in panel.leaves:
                if leaf.kind in ("unmeasured", "identity_unmeasured"):
                    unmeasured += leaf.kind == "unmeasured"
                    unseen_unmeasured += leaf.kind == "identity_unmeasured"
                    notes.append(f"panel {panel.panel_id}: {leaf.field} - {leaf.reason}")
        # No row sets: callers allocate signatures, never id(row).
        return SuppressionSummary(
            unmeasured,
            self.interpretations,
            notes,
            unseen_unmeasured,
        )

    def suppressed(self, mask: int) -> bool:
        return bool(self.panel_masks) and all(
            mask & panel_mask & self.active_mask for panel_mask in self.panel_masks
        )
