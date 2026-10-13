from __future__ import annotations

from datetime import datetime

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
from alerts_bi_operations.report.render import OUTPUT_FILES

from tests.engine.helpers.sql import sample_daily, sample_finding

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


def test_exactly_the_three_approved_csv_exports_are_written() -> None:
    assert list(OUTPUT_FILES) == [
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
