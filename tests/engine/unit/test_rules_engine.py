from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from alerts_bi_shared.window import build_run_window
from src.engine.domain.normalize import AlertRecord, identity_of
from src.engine.rules.core import Finding
from src.engine.rules.engine import (
    Evaluation,
    attach_row_findings,
    compare_rule_ids,
    compute_daily_flagged,
    compute_daily_rule_counts,
    count_phase2_gap_identities,
)
from src.engine.rules.engine import (
    evaluate_rows as _evaluate_rows,
)

from tests.engine.helpers.rows import v1_row, v2_row

# Close to the rows, so a single open Grafana row is never R6 'stuck' as a side effect.
WINDOW_END = datetime(2026, 8, 20, 13, 0, tzinfo=UTC)


def evaluate_rows(rows: Sequence[AlertRecord]) -> Evaluation:
    return _evaluate_rows(rows, WINDOW_END)


DATES = build_run_window(datetime.fromisoformat("2026-08-25T18:00:00+00:00")).snapshot_dates


def test_core_rules_are_evaluated_on_every_raw_row_not_the_representative_alone() -> None:
    # Same identity, three rows; only the middle one is a heartbeat.
    rows = [
        v1_row(
            **{
                "@timestamp": "2026-08-20T10:00:00Z",
                "key_field": "k",
                "message": "Real failure detail",
            }
        ),
        v1_row(**{"@timestamp": "2026-08-20T11:00:00Z", "key_field": "k", "message": "i am alive"}),
        v1_row(
            **{
                "@timestamp": "2026-08-20T12:00:00Z",
                "key_field": "k",
                "message": "Real failure detail",
                # A clear row ends the episode, so this is not R6 spamming.
                "severity": 1,
            }
        ),
    ]
    evaluation = evaluate_rows(rows)
    assert [[f.rule_id for f in e.core_findings] for e in evaluation.rows] == [[], ["R2"], []]


def test_a_finding_is_not_projected_onto_other_rows_sharing_the_identity() -> None:
    rows = [
        v1_row(**{"@timestamp": "2026-08-20T10:00:00Z", "key_field": "k", "message": "i am alive"}),
        v1_row(
            **{
                "@timestamp": "2026-08-21T10:00:00Z",
                "key_field": "k",
                "message": "Real failure detail",
            }
        ),
    ]
    counts = [
        c for c in compute_daily_rule_counts(evaluate_rows(rows).rows, DATES) if c.rule_id == "R2"
    ]
    # Only the date whose row actually matched carries the finding.
    assert len(counts) == 1
    assert counts[0].snapshot_date == "2026-08-20"
    assert (counts[0].match_count, counts[0].distinct_count) == (1, 1)


def test_any_core_finding_anywhere_withholds_the_whole_identity_from_the_llm() -> None:
    rows = [
        v1_row(**{"@timestamp": "2026-08-20T10:00:00Z", "key_field": "k", "message": "i am alive"}),
        v1_row(
            **{
                "@timestamp": "2026-08-21T10:00:00Z",
                "key_field": "k",
                "message": "Real failure detail",
            }
        ),
    ]
    identity = evaluate_rows(rows).identities[identity_of("app-1", "k")]
    assert identity.has_core_finding is True
    assert identity.llm_eligible is False
    assert identity.core_rule_ids == ["R2"]


def test_an_identity_with_no_core_finding_stays_llm_eligible() -> None:
    identity = evaluate_rows([v1_row(key_field="clean")]).identities[identity_of("app-1", "clean")]
    assert identity.has_core_finding is False
    assert identity.llm_eligible is True


def test_v2_readiness_gaps_never_withhold_an_identity_from_the_llm() -> None:
    evaluation = evaluate_rows(
        [v2_row(key_field="gap", impact=None, runbook_url=None, severity="critical")]
    )
    identity = evaluation.identities[identity_of("app-2", "gap")]
    assert identity.readiness_rule_ids == ["R8", "R9"]
    assert identity.has_core_finding is False
    assert identity.llm_eligible is True


def test_readiness_gaps_are_read_off_the_representative_so_enrichment_clears_them() -> None:
    rows = [
        v2_row(**{"@timestamp": "2026-08-20T10:00:00Z", "key_field": "k", "impact": None}),
        v2_row(
            **{"@timestamp": "2026-08-21T10:00:00Z", "key_field": "k", "impact": "Checkout is slow"}
        ),
    ]
    identity = evaluate_rows(rows).identities[identity_of("app-2", "k")]
    assert identity.readiness_rule_ids == []


def test_per_rule_count_is_matching_rows_and_distinct_count_is_matching_identities() -> None:
    rows = [
        # identity A: two matching rows on one date
        v1_row(**{"@timestamp": "2026-08-20T10:00:00Z", "key_field": "a", "message": "i am alive"}),
        v1_row(**{"@timestamp": "2026-08-20T11:00:00Z", "key_field": "a", "message": "i am alive"}),
        # identity B: one matching row on the same date
        v1_row(**{"@timestamp": "2026-08-20T12:00:00Z", "key_field": "b", "message": "healthy"}),
    ]
    counts = compute_daily_rule_counts(evaluate_rows(rows).rows, DATES)
    r2 = next(c for c in counts if c.rule_id == "R2" and c.snapshot_date == "2026-08-20")
    assert r2.match_count == 3
    assert r2.distinct_count == 2


def test_an_identity_is_counted_once_in_each_bucket_where_it_matched() -> None:
    rows = [
        v1_row(**{"@timestamp": "2026-08-20T10:00:00Z", "key_field": "k", "message": "i am alive"}),
        v1_row(**{"@timestamp": "2026-08-21T10:00:00Z", "key_field": "k", "message": "i am alive"}),
    ]
    counts = [
        c for c in compute_daily_rule_counts(evaluate_rows(rows).rows, DATES) if c.rule_id == "R2"
    ]
    assert len(counts) == 2
    assert [c.distinct_count for c in counts] == [1, 1]


def test_flagged_by_rule_counts_a_row_once_however_many_rules_it_matches() -> None:
    rows = [
        v1_row(
            **{
                "@timestamp": "2026-08-20T10:00:00Z",
                "key_field": "a",
                "message": "i am alive",
                "operator": "unknown",
                "alert_rule_url": None,
            }
        ),
        v1_row(**{"@timestamp": "2026-08-20T11:00:00Z", "key_field": "b"}),
    ]
    evaluation = evaluate_rows(rows)
    assert len(evaluation.rows[0].core_findings) == 3
    flagged, distinct = compute_daily_flagged(evaluation.rows, DATES)["2026-08-20"]
    assert flagged == 1
    assert distinct == 1


def test_readiness_gaps_never_enter_flagged_by_rule() -> None:
    rows = [v2_row(**{"@timestamp": "2026-08-20T10:00:00Z", "impact": None, "runbook_url": None})]
    assert compute_daily_flagged(evaluate_rows(rows).rows, DATES)["2026-08-20"] == (0, 0)


def test_readiness_rules_still_appear_in_the_per_rule_breakdown() -> None:
    rows = [v2_row(**{"@timestamp": "2026-08-20T10:00:00Z", "impact": None})]
    counts = compute_daily_rule_counts(evaluate_rows(rows).rows, DATES)
    assert any(c.rule_id == "R8" and c.match_count == 1 for c in counts)


def test_an_externally_attached_r5_becomes_core_and_withholds_the_identity() -> None:
    row = v1_row(**{"@timestamp": "2026-08-20T10:00:00Z", "key_field": "k"})
    evaluation = evaluate_rows([row])
    assert evaluation.identities[identity_of("app-1", "k")].llm_eligible is True

    attach_row_findings(evaluation, {id(row): Finding("R5", "core", {"panel_id": "p1"})})

    identity = evaluation.identities[identity_of("app-1", "k")]
    assert identity.core_rule_ids == ["R5"]
    assert identity.llm_eligible is False
    assert compute_daily_flagged(evaluation.rows, DATES)["2026-08-20"][0] == 1


def test_attaching_r5_to_one_row_leaves_the_other_rows_unmatched() -> None:
    suppressed = v1_row(**{"@timestamp": "2026-08-20T10:00:00Z", "key_field": "k"})
    visible = v1_row(**{"@timestamp": "2026-08-21T10:00:00Z", "key_field": "k"})
    evaluation = evaluate_rows([suppressed, visible])
    attach_row_findings(evaluation, {id(suppressed): Finding("R5", "core", {})})

    counts = [c for c in compute_daily_rule_counts(evaluation.rows, DATES) if c.rule_id == "R5"]
    assert len(counts) == 1
    assert counts[0].snapshot_date == "2026-08-20"
    assert (counts[0].match_count, counts[0].distinct_count) == (1, 1)


def test_identity_records_the_dates_it_appears_on() -> None:
    rows = [
        v1_row(**{"@timestamp": "2026-08-20T10:00:00Z", "key_field": "k"}),
        v1_row(**{"@timestamp": "2026-08-22T10:00:00Z", "key_field": "k"}),
    ]
    identity = evaluate_rows(rows).identities[identity_of("app-1", "k")]
    assert sorted(identity.present_dates) == ["2026-08-20", "2026-08-22"]


def test_phase2_gap_identities_are_counted_from_v2_representatives_only() -> None:
    evaluation = evaluate_rows([v2_row(key_field="a", impact=None), v2_row(key_field="b")])
    assert count_phase2_gap_identities(list(evaluation.identities.values())) == 1


def test_rule_ids_sort_numerically_so_r10_does_not_land_between_r1_and_r2() -> None:
    assert sorted(["R10", "R2", "R1"], key=compare_rule_ids) == ["R1", "R2", "R10"]


def test_the_per_rule_breakdown_is_ordered_by_date_then_rule_number() -> None:
    rows = [
        v2_row(**{"@timestamp": "2026-08-21T10:00:00Z", "key_field": "x", "impact": "high cpu"}),
        v2_row(**{"@timestamp": "2026-08-20T10:00:00Z", "key_field": "y", "message": "i am alive"}),
    ]
    counts = compute_daily_rule_counts(evaluate_rows(rows).rows, DATES)
    assert [f"{c.snapshot_date}:{c.rule_id}" for c in counts] == [
        "2026-08-20:R2",
        "2026-08-21:R10",
    ]
