"""The exact wording of every catalogue string a person or the model reads.

These strings are the company standard's wording. They reach the LLM prompt and the
scorecard verbatim, and they are versioned by ``RULESET_VERSION`` / ``PROMPT_VERSION``, so a
silent edit changes what the model was asked and what a reader was told without changing the
version that is supposed to describe it.

Pinned here rather than left to review because the wording did drift once: the port to
Python replaced the em dash in eight of these strings with a hyphen.
"""

from __future__ import annotations

import pytest
from alerts_bi_shared.catalogs import (
    CITABLE_IDS,
    PRINCIPLE_CATALOG,
    PRINCIPLE_IDS,
)
from alerts_bi_shared.phase import PHASE_LABELS

EXPECTED_PRINCIPLES = {
    "P1": "Non-actionable — implies no investigation, fix, escalation or attention",
    "P2": 'Informational — reports an event or a status rather than a problem ("that\'s a log!")',
    "P3": "States the outcome, not the failure — something failed, but not what",
    "P4": "Component or application name does not identify a real thing",
    "P5": "No environment context — the reader cannot tell where it fired",
    "P6": "Not grounded in a golden signal (latency / traffic / errors / saturation)",
    "P7": "critical that fails the Wake-Up Test (urgent + immediate damage + runbook)",
    "P8": "Severity is not derived from impact — the two are incoherent",
    "P9": "impact restates the technical cause rather than the operational symptom",
    "P10": "The required response is robotic and should have been automated, not alerted",
    "P11": "An internal technical cause with no user-visible symptom anywhere in the alert",
}

EXPECTED_PHASE_LABELS = {
    "no_data": "No data in this window",
    "phase_0": "Phase 0 — Clean",
    "phase_1": "Phase 1 — New rules (dual-run)",
    "phase_2": "Phase 2 — Enrich",
    "done": "Done",
}


@pytest.mark.parametrize(("principle_id", "text"), sorted(EXPECTED_PRINCIPLES.items()))
def test_each_principle_reads_exactly_as_the_standard_words_it(
    principle_id: str, text: str
) -> None:
    catalog = {entry.id: entry.text for entry in PRINCIPLE_CATALOG}
    assert catalog[principle_id] == text


def test_the_catalogue_holds_exactly_these_principles_and_no_others() -> None:
    assert sorted(PRINCIPLE_IDS, key=lambda i: int(i[1:])) == sorted(
        EXPECTED_PRINCIPLES, key=lambda i: int(i[1:])
    )


@pytest.mark.parametrize(("phase", "label"), sorted(EXPECTED_PHASE_LABELS.items()))
def test_each_phase_label_reads_exactly_as_the_scorecard_shows_it(phase: str, label: str) -> None:
    assert PHASE_LABELS[phase] == label


def test_every_principle_is_citable_by_the_model() -> None:
    for principle_id in EXPECTED_PRINCIPLES:
        assert principle_id in CITABLE_IDS


def test_no_catalogue_string_uses_a_hyphen_where_the_standard_uses_an_em_dash() -> None:
    """The drift that motivated this file: an em dash quietly became a hyphen."""
    dashed = [entry.id for entry in PRINCIPLE_CATALOG if " - " in entry.text]
    assert dashed == [], f"principles using a spaced hyphen: {dashed}"
    assert [label for label in PHASE_LABELS.values() if " - " in label] == []
