"""Migration phase derivation (design section 3.4; flow step 7).

The phase is derived from distinct identity presence and phase-2 readiness, never
self-reported: self-reported progress drifts optimistic.

``no_data`` exists so that an empty seven-day window is not read as proof that a team has
not migrated. And v1 reaching zero does not by itself mark a team done - phase-2 readiness
must also be complete.

Like every phase conclusion here, this describes only alerts that fired in the measured
week. It cannot see silent rules or an external inventory.
"""

from __future__ import annotations

from typing import Final, Literal

__all__ = ["PHASE_LABELS", "Phase", "derive_phase"]

Phase = Literal["no_data", "phase_0", "phase_1", "phase_2", "done"]


def derive_phase(
    v1_identity_count: int,
    v2_identity_count: int,
    phase2_readiness_pct: float | None,
) -> Phase:
    """Derive the phase exhaustively from identity presence and readiness."""
    has_v1 = v1_identity_count > 0
    has_v2 = v2_identity_count > 0

    if not has_v1 and not has_v2:
        return "no_data"
    if has_v1 and not has_v2:
        return "phase_0"
    if has_v1 and has_v2:
        return "phase_1"
    # v2 only from here.
    if phase2_readiness_pct is not None and phase2_readiness_pct >= 100:
        return "done"
    return "phase_2"


#: Human-readable phase labels for the scorecard.
PHASE_LABELS: Final[dict[str, str]] = {
    "no_data": "No data in this window",
    "phase_0": "Phase 0 — Clean",
    "phase_1": "Phase 1 — New rules (dual-run)",
    "phase_2": "Phase 2 — Enrich",
    "done": "Done",
}
