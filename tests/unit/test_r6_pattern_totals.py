import pytest
from alerts_bi_shared.ui.explain import dominant_r6_pattern, dominant_r6_pattern_totals


@pytest.mark.parametrize(
    ("totals", "expected"),
    [
        ({"spamming": [10, 1000], "stuck": [1, 2000]}, "spamming"),
        ({"spamming": [1, 1000], "stuck": [10, 2000]}, "stuck"),
        ({"spamming": [2, 1000], "stuck": [2, 2000]}, "stuck"),
        ({"spamming": [2, 1000], "stuck": [2, 1000], "flapping": [2, 1000]}, "flapping"),
        ({"spamming": [2, 1000], "stuck": [2, 1000]}, "spamming"),
        ({"bogus": [100, 100000], "stuck": [0, 0]}, None),
        ({}, None),
    ],
)
def test_stored_pattern_ranking_preserves_alert_counts_and_tie_breaks(
    totals: dict[str, list[int]], expected: str | None
) -> None:
    assert dominant_r6_pattern_totals(totals) == expected


def test_aggregate_and_full_input_ranking_agree_when_event_and_alert_rankings_differ() -> None:
    full = [("spamming", 100)] * 10 + [("stuck", 2000)]
    assert dominant_r6_pattern(full) == dominant_r6_pattern_totals(
        {"spamming": [10, 1000], "stuck": [1, 2000]}
    )
