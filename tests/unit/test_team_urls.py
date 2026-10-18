"""Team IDs survive routing without normalization or ambiguous action suffixes."""

import pytest
from alerts_bi_shared.team_urls import decode_team_id, encode_team_id


@pytest.mark.parametrize(
    "team",
    [
        "team-a",
        "Team_A",
        "team/a",
        "team/a/summary",
        "a%2Fb",
        "~61",
        ".",
        "..",
        "צוות",
        "Team A",
        "a\\b",
        "a?b#c",
    ],
)
def test_team_url_round_trip(team: str) -> None:
    segment = encode_team_id(team)
    assert "/" not in segment and "%" not in segment
    assert segment not in (".", "..")
    assert decode_team_id(segment) == team


def test_existing_slug_links_are_unchanged() -> None:
    assert encode_team_id("checkout-api") == "checkout-api"


@pytest.mark.parametrize("segment", ["~", "~ff", "~6", "~61", "~2F", "~20 2f"])
def test_malformed_or_noncanonical_tokens_are_rejected(segment: str) -> None:
    with pytest.raises(ValueError):
        decode_team_id(segment)


def test_global_trigger_selector_carries_server_encoded_actions() -> None:
    from src.pages import trigger_form

    markup = trigger_form(
        "team/a", "csrf", teams=[("team/a", "Slash team", False), ("a%2Fb", "Percent team", False)]
    )
    assert 'value="team/a" data-trigger-url="/teams/~7465616d2f61/trigger"' in markup
    assert 'value="a%2Fb" data-trigger-url="/teams/~6125324662/trigger"' in markup
