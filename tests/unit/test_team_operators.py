"""The team page lists the team's operators (design "Team operators on the team page")."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from alerts_bi_operations.registry import TeamEntry
from src import pages
from src.summary import snapshot_operators

LEGACY = TeamEntry("legacy", "Legacy Batch Jobs", ("batch-team", "BATCH_JOBS", "<b>"), None)
CHECKOUT = TeamEntry("checkout", "Checkout", ("platform-ops", "batch-team"), "checkout")
FULFILMENT = TeamEntry("fulfilment", "Fulfilment", ("platform-ops",), "BATCH_JOBS")
EMPTY = TeamEntry("search", "Search", (), None)
TEAMS = (LEGACY, CHECKOUT, FULFILMENT, EMPTY)
RUN = {
    "window_start": datetime(2026, 10, 12, tzinfo=UTC),
    "window_end": datetime(2026, 10, 19, tzinfo=UTC),
    "registry_version": "2026-09-15.2",
}


def _panel(team_id: str, frozen: Any = None, run: Any = RUN) -> str:
    return pages.operators_panel(team_id, TEAMS, "2026-10-01.1", "2026-10-01", run, frozen)


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_lists_every_current_operator_exactly_and_escaped() -> None:
    html = _panel("legacy", frozen=(LEGACY.v1_operators, None))
    assert "v1 operators (3)" in html
    chips = re.findall(r'<span class="abi-chip abi-mono">([^<]*)</span>', html)
    assert chips == ["batch-team", "BATCH_JOBS", "&lt;b&gt;"]
    assert "Not set yet" in html
    assert "Registry 2026-10-01.1 · effective 1 Oct 2026" in html
    assert "abi-note" not in html


def test_notes_when_the_selected_week_ran_with_a_different_list() -> None:
    html = _text(_panel("legacy", frozen=(("batch-team",), None)))
    assert (
        "The selected week (12 Oct \u2013 19 Oct 2026) ran with registry 2026-09-15.2, which listed "
        "1 operator: batch-team. Run the week again to use the current list."
    ) in html
    assert "which listed no operators" in _text(_panel("legacy", frozen=((), None)))


def test_an_unknown_snapshot_or_no_run_adds_no_note() -> None:
    assert "abi-note" not in _panel("legacy", frozen=None)
    assert "abi-note" not in _panel("legacy", frozen=None, run=None)


def test_names_other_teams_only_within_the_same_schema() -> None:
    html = _text(_panel("checkout"))
    assert "platform-ops is also listed for Fulfilment." in html
    assert "batch-team is also listed for Legacy Batch Jobs." in html
    # Fulfilment's v2 BATCH_JOBS is not Legacy's v1 BATCH_JOBS: different sources.
    assert "BATCH_JOBS is also listed" not in _text(_panel("legacy"))


def test_a_team_without_operators_says_runs_find_nothing() -> None:
    html = _text(_panel("search"))
    assert "v1 operators (0)" in html and "None" in html and "Not set yet" in html
    assert "so a run finds no alerts for it" in html


def test_a_team_missing_from_the_registry_shows_what_its_run_used() -> None:
    html = _text(_panel("retired", frozen=(("old-op",), "old-v2")))
    assert "This team is not in the current registry." in html
    assert "These are the operators the selected week ran with." in html
    assert "old-op" in html and "old-v2" in html
    bare = _text(_panel("retired"))
    assert "This team is not in the current registry." in bare
    assert "v1 operators" not in bare


def test_team_page_places_operators_above_the_week() -> None:
    html = pages.team_page(
        "legacy",
        "Legacy",
        True,
        [],
        0,
        1,
        None,
        "<p>STEP</p>",
        None,
        False,
        operators="<section>OPERATORS</section>",
    )
    assert html.index("OPERATORS") < html.index("STEP")


def test_snapshot_operators_reads_only_a_listed_entry() -> None:
    entry = {"team_id": "x", "v1_operators": ["a", "A"], "v2_operator": "b"}
    assert snapshot_operators(json.dumps(entry)) == (("a", "A"), "b")
    assert snapshot_operators(json.dumps({**entry, "v2_operator": None})) == (("a", "A"), None)
    assert snapshot_operators('{"team_id":"x"}') is None
    assert snapshot_operators("not json") is None
    assert snapshot_operators(json.dumps({**entry, "v1_operators": ["a", None]})) is None
    assert snapshot_operators(None) is None
