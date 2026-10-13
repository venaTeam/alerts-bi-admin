"""The page's styling, guarded where getting it wrong hides the UI.

A native ``<select>`` popup is painted by the browser on its own surface. Leaving the
option background ``transparent`` put white option text on a light popup in dark mode and
made every team but the pre-selected one invisible - the form looked like it offered only
one team. Nothing in the markup was wrong, so only a style assertion catches it.
"""

from __future__ import annotations

import re

import pytest
from src.engine.api.schemas import TeamOut
from src.engine.api.ui import index_page
from src.engine.api.ui.assets import PAGE_CSS, SUBMIT_SCRIPT

TOKENS = ("--bg", "--fg", "--field-bg", "--line", "--muted", "--error")

_DARK_MARKER = "prefers-color-scheme: dark"

#: Comments explain the bug and name `transparent`; only declarations are asserted on.
_DECLARATIONS = re.sub(r"/\*.*?\*/", "", PAGE_CSS, flags=re.DOTALL)
_LIGHT = _DECLARATIONS[: _DECLARATIONS.index(_DARK_MARKER)]
_DARK = _DECLARATIONS[_DECLARATIONS.index(_DARK_MARKER) :]


@pytest.mark.parametrize("token", TOKENS)
def test_every_colour_token_has_a_value_in_both_schemes(token: str) -> None:
    assert f"{token}:" in _LIGHT, f"{token} has no light value"
    assert f"{token}:" in _DARK, f"{token} has no dark value"


def test_no_colour_is_left_transparent() -> None:
    """The bug: a transparent popup surface under light-on-dark option text."""
    assert "transparent" not in _DECLARATIONS


def test_options_carry_an_explicit_background_and_colour() -> None:
    assert "option { background: var(--field-bg); color: var(--fg); }" in PAGE_CSS


def test_form_controls_carry_an_explicit_background_and_colour() -> None:
    assert "background: var(--field-bg); color: var(--fg);" in PAGE_CSS


def test_the_page_paints_its_own_background_rather_than_borrowing_one() -> None:
    assert "background: var(--bg); color: var(--fg);" in PAGE_CSS


def test_a_failed_request_re_enables_the_button_instead_of_hanging_on_running() -> None:
    """A dead server must say so; a stuck 'Running' reads as a slow run."""
    assert "catch" in SUBMIT_SCRIPT
    assert "button.disabled = false" in SUBMIT_SCRIPT
    assert "Could not reach the service" in SUBMIT_SCRIPT


def test_the_submit_handler_reports_into_a_slot_the_page_actually_has() -> None:
    assert "getElementById('failure')" in SUBMIT_SCRIPT
    page = index_page(
        [TeamOut(team_id="t", display_name="T", v1_operators=["o"], v2_operator=None, panels=0)]
    )
    assert 'id="failure"' in page


def test_every_registered_team_reaches_the_dropdown() -> None:
    """The reported symptom was a dropdown that appeared to hold one team."""
    teams = [
        TeamOut(
            team_id=f"team-{i}",
            display_name=f"Team {i}",
            v1_operators=[f"op-{i}"],
            v2_operator=None,
            panels=0,
        )
        for i in range(11)
    ]
    page = index_page(teams)
    for team in teams:
        assert f'<option value="{team.team_id}">' in page


# ------------------------------------------------------------- CSV results


def test_completed_runs_offer_csvs_without_html_downloads() -> None:
    assert "Accept: 'application/json'" in SUBMIT_SCRIPT
    assert "daily_metrics.csv" in SUBMIT_SCRIPT
    assert "rule_counts.csv" in SUBMIT_SCRIPT
    assert "alert_worklist.csv" in SUBMIT_SCRIPT
    assert "createObjectURL" not in SUBMIT_SCRIPT
    assert "text/html" not in SUBMIT_SCRIPT
    assert "innerHTML" not in SUBMIT_SCRIPT
    assert "failure.textContent" in SUBMIT_SCRIPT


def test_the_button_is_restored_on_every_path() -> None:
    """finally, so a refusal leaves the form as usable as a success does."""
    assert "} finally {" in SUBMIT_SCRIPT
    assert SUBMIT_SCRIPT.count("button.disabled = false") == 1


def test_the_page_provides_the_result_slot_the_handler_writes_into() -> None:
    assert "getElementById('result')" in SUBMIT_SCRIPT
    page = index_page(
        [TeamOut(team_id="t", display_name="T", v1_operators=["o"], v2_operator=None, panels=0)]
    )
    assert 'id="result"' in page
