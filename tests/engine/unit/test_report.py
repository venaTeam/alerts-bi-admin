from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pytest
from alerts_bi_operations.report.csv_export import (
    DAILY_METRIC_HEADERS,
    WORKLIST_HEADERS,
    alert_worklist_csv,
    csv_cell,
    daily_metrics_csv,
    rule_counts_csv,
    to_csv,
)
from alerts_bi_operations.report.html import escape_html, render_scorecard, rollup_schema
from alerts_bi_operations.report.render import OUTPUT_FILES

from tests.engine.helpers.sql import sample_daily, sample_finding, sample_run

# ----------------------------------------------------------------- CSV safety


@pytest.mark.parametrize("dangerous", ["=1+1", "+1", "-1", "@SUM(A1)", "=cmd|' /c calc'!A0"])
def test_formula_prefixes_are_neutralized(dangerous: str) -> None:
    assert csv_cell(dangerous).startswith("'")


def test_a_leading_formula_character_inside_the_text_is_left_alone() -> None:
    assert csv_cell("cpu = 90%") == "cpu = 90%"


def test_quotes_commas_and_newlines_are_escaped_rather_than_breaking_the_row() -> None:
    assert to_csv(["a"], [["a,b"]]).endswith('"a,b"\r\n')
    assert to_csv(["a"], [['say "hi"']]).endswith('"say ""hi"""\r\n')
    assert '"line1\nline2"' in to_csv(["a"], [["line1\nline2"]])


def test_none_becomes_an_empty_cell_not_the_string_none() -> None:
    assert csv_cell(None) == ""


def test_dates_are_written_as_iso_instants() -> None:
    assert csv_cell(datetime(2026, 8, 20, 12, 0, 0)) == "2026-08-20T12:00:00.000Z"


def test_rows_are_joined_with_crlf_per_rfc_4180() -> None:
    assert to_csv(["a", "b"], [[1, 2]]) == "a,b\r\n1,2\r\n"


def test_a_neutralized_cell_that_also_needs_quoting_gets_both() -> None:
    assert to_csv(["a"], [["=a,b"]]).endswith('"\'=a,b"\r\n')


# --------------------------------------------------------------- CSV contract


def test_exactly_the_three_approved_exports_plus_the_scorecard_are_written() -> None:
    assert list(OUTPUT_FILES) == [
        "scorecard.html",
        "daily_metrics.csv",
        "rule_counts.csv",
        "alert_worklist.csv",
    ]


def test_daily_metrics_csv_carries_every_stored_metric_column() -> None:
    headers = daily_metrics_csv([sample_daily()]).split("\r\n")[0].split(",")
    for column in (
        "snapshot_date",
        "covered_hours",
        "alerts",
        "distinct_alerts",
        "alerts_per_hour",
        "node_name_numerator",
        "node_name_denominator",
        "node_name_ratio",
        "key_inflation_numerator",
        "key_inflation_denominator",
        "key_inflation_ratio",
        "flagged_by_rule",
        "flagged_by_rule_distinct",
        "flagged_by_llm",
        "flagged_by_llm_distinct",
        "needs_review",
        "assessed_good",
        "unassessed",
        "phase2_gaps",
        "suppressed",
        "suppression_unmeasured",
    ):
        assert column in headers, f"missing column {column}"


def test_rule_counts_csv_carries_the_ruleset_version_with_every_count() -> None:
    csv = rule_counts_csv(
        [
            {
                "run_id": "r",
                "team_id": "t",
                "alert_schema": "v1",
                "snapshot_date": "2026-08-20",
                "rule_id": "R2",
                "ruleset_version": "1.0.0",
                "match_count": 3,
                "distinct_count": 1,
            }
        ]
    )
    assert "R2,1.0.0,3,1" in csv


def test_the_work_list_keeps_full_values_including_a_long_justification() -> None:
    long_text = "x" * 900
    assert long_text in alert_worklist_csv([sample_finding(llm_justification=long_text)])


def test_a_work_list_message_containing_a_comma_stays_one_field() -> None:
    csv = alert_worklist_csv([sample_finding(message="cart failed, retries exhausted")])
    assert '"cart failed, retries exhausted"' in csv


# ------------------------------------------------------ team-summary columns


def test_daily_metrics_headers_end_with_the_unseen_pair_after_suppression() -> None:
    assert DAILY_METRIC_HEADERS[-3:] == (
        "suppression_unmeasured",
        "unseen",
        "unseen_unmeasured",
    )
    assert len(DAILY_METRIC_HEADERS) == 28


def test_worklist_headers_end_with_the_r6_facts_and_unseen() -> None:
    assert WORKLIST_HEADERS[-6:] == (
        "clear_count",
        "max_clear_cycles_24h",
        "fire_pattern",
        "unseen",
        "max_episode_firing_rows",
        "open_since",
    )
    assert len(WORKLIST_HEADERS) == 27


def test_a_null_unseen_is_an_empty_daily_cell_not_zero() -> None:
    line = daily_metrics_csv([sample_daily(unseen=None, unseen_unmeasured=None)]).split("\r\n")[1]
    assert line.endswith(",0,,"), line


def test_a_stored_unseen_count_is_written() -> None:
    line = daily_metrics_csv([sample_daily(unseen=3, unseen_unmeasured=1)]).split("\r\n")[1]
    assert line.endswith(",0,3,1"), line


def test_worklist_writes_the_r6_facts_and_renders_booleans_as_true_false() -> None:
    rows = alert_worklist_csv(
        [
            sample_finding(
                clear_count=2, max_clear_cycles_24h=3, fire_pattern="flapping", unseen=True
            ),
            sample_finding(clear_count=0, max_clear_cycles_24h=0, fire_pattern=None, unseen=None),
            sample_finding(unseen=False),
        ]
    ).split("\r\n")
    assert rows[1].endswith(",2,3,flapping,true,0,")
    assert rows[2].endswith(",0,0,,,0,")
    assert rows[3].endswith(",false,0,")


def test_worklist_writes_the_episode_facts() -> None:
    row = alert_worklist_csv(
        [sample_finding(max_episode_firing_rows=4, open_since=datetime(2026, 8, 21, 3, 0, 0))]
    ).split("\r\n")[1]
    assert row.endswith(",4,2026-08-21T03:00:00.000Z")


def test_the_rollup_sums_unseen_and_keeps_none_when_no_bucket_has_a_panel() -> None:
    both = rollup_schema([sample_daily(unseen=2), sample_daily(unseen=1, unseen_unmeasured=4)])
    assert both["unseen"] == 3
    assert both["unseen_unmeasured"] == 4
    none = rollup_schema([sample_daily(unseen=None), sample_daily(unseen=None)])
    assert none["unseen"] is None
    mixed = rollup_schema([sample_daily(unseen=None), sample_daily(unseen=5)])
    assert mixed["unseen"] == 5


def _card_html(html: str, label: str) -> str:
    start = html.index(label)
    return html[start : html.index("</div></div>", start)]


def test_the_visibility_section_shows_unseen_per_schema_never_summed() -> None:
    html = scorecard(
        daily=[
            sample_daily(unseen=3, unseen_unmeasured=1),
            sample_daily(alert_schema="v2", unseen=4, unseen_unmeasured=2),
        ]
    )
    card = _card_html(html, "Unseen (rows no panel shows)")
    assert "v1: 3" in card and "v2: 4" in card
    assert "7" not in card
    unm = _card_html(html, "Unseen unmeasured (leaves)")
    assert "v1: 1" in unm and "v2: 2" in unm


def test_an_unseen_schema_without_a_panel_says_so_in_its_own_card() -> None:
    html = scorecard(
        daily=[
            sample_daily(unseen=3, unseen_unmeasured=None),
            sample_daily(alert_schema="v2", unseen=None, unseen_unmeasured=None),
        ]
    )
    card = _card_html(html, "Unseen (rows no panel shows)")
    assert "v1: 3" in card
    assert "v2: &mdash; no panel supplied" in card
    unm = _card_html(html, "Unseen unmeasured (leaves)")
    assert "v1: &mdash; no panel supplied" in unm
    assert "v2: &mdash; no panel supplied" in unm


def test_the_rollup_keeps_unseen_unmeasured_none_when_every_bucket_is_none() -> None:
    assert rollup_schema([sample_daily(unseen_unmeasured=None)])["unseen_unmeasured"] is None
    assert rollup_schema([sample_daily(unseen_unmeasured=0)])["unseen_unmeasured"] == 0


def test_the_limitations_say_r6_flags_one_alert_never_team_volume() -> None:
    flat = " ".join(scorecard().split())
    assert (
        "R6 flags one alert's firing episodes (stuck, spamming or flapping); it never "
        "scores a team's total volume." in flat
    )


# -------------------------------------------------------------- HTML escaping


def test_html_escaping_neutralizes_markup_in_alert_content() -> None:
    assert escape_html('<script>alert("x")</script>') == (
        "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;"
    )
    assert escape_html("it's") == "it&#39;s", "decimal, so a scorecard stays byte-comparable"
    assert escape_html("a & b") == "a &amp; b"


def test_a_hostile_alert_message_cannot_inject_markup_into_the_scorecard() -> None:
    html = render_scorecard(
        sample_run(),
        [sample_daily()],
        [],
        [
            sample_finding(
                quality_state="rule_flagged",
                core_rule_ids="R2",
                message='<img src=x onerror="alert(1)">',
                application="</td></tr><script>bad()</script>",
            )
        ],
        [],
        [],
    )
    assert "<img src=x" not in html
    assert "<script>bad()</script>" not in html
    assert "&lt;img src=x" in html


# ------------------------------------------------------------------- rollups


def test_the_scorecard_rollup_sums_rows_and_divides_distinct_by_seven() -> None:
    rollup = rollup_schema(
        [
            sample_daily(snapshot_date="2026-08-20", alerts=100, distinct_alerts=3),
            sample_daily(snapshot_date="2026-08-21", alerts=68, distinct_alerts=4),
        ]
    )
    assert rollup["alerts"] == 168
    assert rollup["alerts_per_hour"] == 1, "divides by 168 hours, not by covered hours"
    assert rollup["distinct_per_day"] == 1


def test_diagnostic_rollups_divide_summed_numerators_by_summed_denominators() -> None:
    rollup = rollup_schema(
        [
            sample_daily(node_name_numerator=2, node_name_denominator=1),
            sample_daily(
                snapshot_date="2026-08-21", node_name_numerator=1, node_name_denominator=1
            ),
        ]
    )
    assert rollup["node_name_ratio"] == 1.5, "not the mean of 2 and 1"


def test_a_zero_denominator_rolls_up_to_none_never_zero() -> None:
    rollup = rollup_schema(
        [sample_daily(node_name_numerator=0, node_name_denominator=0, key_inflation_denominator=0)]
    )
    assert rollup["node_name_ratio"] is None
    assert rollup["key_inflation_ratio"] is None


# ------------------------------------------------------------ scorecard shape


def scorecard(**overrides: Any) -> str:
    return render_scorecard(
        sample_run(**overrides.get("run", {})),
        overrides.get(
            "daily",
            [sample_daily(), sample_daily(alert_schema="v2", alerts=2, distinct_alerts=2)],
        ),
        overrides.get("rule_counts", []),
        overrides.get("findings", [sample_finding()]),
        overrides.get("panels", []),
        overrides.get("attempts", []),
    )


@pytest.mark.parametrize(
    "heading",
    [
        "Run metadata",
        "Migration phase",
        "Volume",
        "Data-quality diagnostics",
        "Quality",
        "Dashboard visibility",
        "Rule and principle breakdown",
        "Daily breakdown",
        "Work list",
        "Limitations",
    ],
)
def test_the_scorecard_contains_every_required_section(heading: str) -> None:
    assert heading in scorecard()


def test_the_scorecard_is_self_contained_with_no_external_resources() -> None:
    html = scorecard()
    assert not re.search(r"<script", html, re.IGNORECASE), "no scripts at all"
    assert not re.search(r"src=[\"']https?:", html, re.IGNORECASE)
    assert not re.search(r"<link[^>]+stylesheet", html, re.IGNORECASE)
    assert "<style>" in html


@pytest.mark.parametrize(
    "label",
    [
        "Registry version",
        "Registry SHA-256",
        "Ruleset version",
        "Prompt version",
        "Model version",
        "Run id",
    ],
)
def test_the_scorecard_shows_metadata_so_a_result_is_reproducible(label: str) -> None:
    assert label in scorecard()


def test_the_scorecard_carries_no_cross_run_comparison_language() -> None:
    html = scorecard()
    assert "no trend, delta, baseline or improvement percentage" in html
    for forbidden in ("previous run", "last week", "leaderboard"):
        assert not re.search(rf"(compared|versus|vs\.?)[^.]{{0,40}}{forbidden}", html, re.I)


def test_the_scorecard_separates_v1_and_v2_volume_rather_than_combining_it() -> None:
    html = scorecard()
    assert "v1 (Appchi)" in html
    assert "v2 (Appchi V2)" in html
    assert "Row counts are never compared across schemas" in html


def test_a_run_without_the_model_states_plainly_that_nothing_was_examined() -> None:
    html = scorecard(
        run={"llm_assessed": False, "model_version": None},
        daily=[sample_daily(assessed_good=0, unassessed=3)],
    )
    assert "not examined" in html
    assert "unassessed" in html


def test_phase2_readiness_renders_as_a_dash_when_there_are_no_v2_identities() -> None:
    html = scorecard(run={"phase2_readiness_pct": None, "phase_derived": "phase_0"})
    assert "Phase-2 readiness" in html
    assert "—" in html


def test_a_team_with_no_panels_says_so_instead_of_showing_an_empty_table() -> None:
    assert "No panel queries were supplied" in scorecard(panels=[])
