"""The acceptance verifier's comparison logic, on synthetic persisted rows.

The full verifier needs the mock stack (tests/acceptance). These pin the parts that do not:
NULL-aware daily and total comparison, and the per-alert ``fire_patterns`` and ``unseen``
blocks of the manifest (team summary spec sections 5 and 6).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.engine.run.verify import VerificationResult, _verify_team


def _day(schema: str, snapshot_date: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "alert_schema": schema,
        "snapshot_date": snapshot_date,
        "alerts": 0,
        "distinct_alerts": 0,
        "flagged_by_rule": 0,
        "flagged_by_rule_distinct": 0,
        "flagged_by_llm": 0,
        "flagged_by_llm_distinct": 0,
        "needs_review": 0,
        "assessed_good": 0,
        "unassessed": 0,
        "phase2_gaps": 0,
        "suppressed": 0,
        "suppression_unmeasured": 0,
        "node_name_numerator": 0,
        "node_name_denominator": 0,
        "key_inflation_numerator": 0,
        "key_inflation_denominator": 0,
        "unseen": None,
        "unseen_unmeasured": None,
    }
    row.update(overrides)
    return row


def _finding(schema: str, key_field: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "alert_schema": schema,
        "application": "app",
        "key_field": key_field,
        "quality_state": "assessed_good",
        "core_rule_ids": "",
        "node_name": None,
        "clear_count": 0,
        "max_clear_cycles_24h": 0,
        "fire_pattern": None,
        "max_episode_firing_rows": 0,
        "open_since": None,
        "unseen": None,
    }
    row.update(overrides)
    return row


_RUN = {"phase_derived": "phase_1", "phase2_readiness_pct": None}


def _verify(
    expected: dict[str, Any],
    daily: list[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> VerificationResult:
    check = VerificationResult()
    manifest = {
        "phase_derived": "phase_1",
        "phase2_readiness_pct": None,
        "identity_states": {"assessed_good": len(findings)},
        **expected,
    }
    _verify_team(check, "team", manifest, _RUN, daily, [], findings, [])
    return check


def _failures(check: VerificationResult) -> list[str]:
    return [failure.where for failure in check.failures]


def test_a_null_daily_value_matches_a_manifest_null() -> None:
    expected = {"schemas": {"v2": {"identities": 0, "daily": {"2026-08-20": {"unseen": None}}}}}
    check = _verify(expected, [_day("v2", "2026-08-20")], [])
    assert check.ok, check.failures


def test_a_null_daily_value_against_a_manifest_number_is_a_mismatch_not_a_crash() -> None:
    expected = {"schemas": {"v1": {"identities": 0, "daily": {"2026-08-20": {"unseen": 0}}}}}
    check = _verify(expected, [_day("v1", "2026-08-20")], [])
    assert _failures(check) == ["team.v1.daily.2026-08-20.unseen"]
    assert check.failures[0].actual is None


def test_a_number_against_a_manifest_null_is_a_mismatch() -> None:
    expected = {"schemas": {"v1": {"identities": 0, "daily": {"2026-08-20": {"unseen": None}}}}}
    check = _verify(expected, [_day("v1", "2026-08-20", unseen=0)], [])
    assert _failures(check) == ["team.v1.daily.2026-08-20.unseen"]


def test_unseen_totals_sum_the_days_and_stay_null_without_a_panel() -> None:
    daily = [
        _day("v1", "2026-08-18", unseen=0, unseen_unmeasured=1),
        _day("v1", "2026-08-20", unseen=1, unseen_unmeasured=0),
        _day("v1", "2026-08-21", unseen=1, unseen_unmeasured=0),
        _day("v2", "2026-08-18"),
        _day("v2", "2026-08-20"),
    ]
    expected = {
        "schemas": {
            "v1": {"identities": 0, "totals": {"unseen": 2, "unseen_unmeasured": 1}},
            "v2": {"identities": 0, "totals": {"unseen": None, "unseen_unmeasured": None}},
        },
        "unseen": {
            "v1": {"unseen_rows": 2, "unseen_unmeasured": 1},
            "v2": {"unseen_rows": None, "unseen_unmeasured": None},
        },
    }
    check = _verify(expected, daily, [])
    assert check.ok, check.failures

    wrong = {
        "schemas": {"v2": {"identities": 0, "totals": {"unseen": 0}}},
        "unseen": {"v2": {"unseen_rows": 0}},
    }
    check = _verify(wrong, daily, [])
    assert _failures(check) == ["team.v2.totals.unseen", "team.v2.unseen.unseen_rows"]


def test_fire_patterns_are_compared_per_alert() -> None:
    # open_since comes back from DATETIME2 as a naive UTC datetime.
    findings = [
        _finding(
            "v1",
            "k-stuck",
            fire_pattern="stuck",
            max_episode_firing_rows=1,
            open_since=datetime(2026, 8, 22, 18, 0),
        ),
        _finding(
            "v1",
            "k-flap",
            fire_pattern="flapping",
            clear_count=3,
            max_clear_cycles_24h=3,
            max_episode_firing_rows=3,
        ),
        _finding("v1", "k-none", max_episode_firing_rows=2),
    ]
    expected = {
        "fire_patterns": {
            "v1": {
                "k-stuck": {
                    "fire_pattern": "stuck",
                    "clear_count": 0,
                    "max_clear_cycles_24h": 0,
                    "max_episode_firing_rows": 1,
                    "open_since": "2026-08-22T18:00:00.000Z",
                },
                "k-flap": {
                    "fire_pattern": "flapping",
                    "clear_count": 3,
                    "max_clear_cycles_24h": 3,
                    "max_episode_firing_rows": 3,
                    "open_since": None,
                },
                "k-none": {
                    "fire_pattern": None,
                    "clear_count": 0,
                    "max_clear_cycles_24h": 0,
                    "max_episode_firing_rows": 2,
                    "open_since": None,
                },
            }
        }
    }
    check = _verify(expected, [], findings)
    assert check.ok, check.failures

    findings[0]["fire_pattern"] = "spamming"
    findings[0]["open_since"] = datetime(2026, 8, 22, 18, 1)
    findings[1]["max_clear_cycles_24h"] = 2
    findings[1]["max_episode_firing_rows"] = 2
    findings[2]["fire_pattern"] = "stuck"
    findings[2]["open_since"] = datetime(2026, 8, 23, 12, 0)
    check = _verify(expected, [], findings)
    assert _failures(check) == [
        "team.v1.fire_patterns.k-stuck.fire_pattern",
        "team.v1.fire_patterns.k-stuck.open_since",
        "team.v1.fire_patterns.k-flap.max_clear_cycles_24h",
        "team.v1.fire_patterns.k-flap.max_episode_firing_rows",
        "team.v1.fire_patterns.k-none.fire_pattern",
        "team.v1.fire_patterns.k-none.open_since",
    ]


def test_a_fact_the_work_list_lacks_is_a_mismatch_not_a_crash() -> None:
    finding = _finding("v1", "k")
    del finding["max_episode_firing_rows"]
    expected = {"fire_patterns": {"v1": {"k": {"max_episode_firing_rows": 1}}}}
    check = _verify(expected, [], [finding])
    assert _failures(check) == ["team.v1.fire_patterns.k.max_episode_firing_rows"]
    assert check.failures[0].actual is None


def test_an_alert_the_manifest_lists_but_the_work_list_lacks_fails() -> None:
    expected = {
        "fire_patterns": {"v1": {"k-gone": {"fire_pattern": "stuck"}}},
        "unseen": {"v2": {"worklist_unseen": {"k-gone-v2": None}}},
    }
    check = _verify(expected, [], [])
    assert _failures(check) == [
        "team.v1.fire_patterns.k-gone",
        "team.v2.unseen.worklist.k-gone-v2",
    ]


def test_worklist_unseen_reads_a_bit_column_and_keeps_null_distinct_from_false() -> None:
    findings = [
        _finding("v1", "k-unseen", unseen=1),
        _finding("v1", "k-seen", unseen=False),
        _finding("v2", "k-no-panel", unseen=None),
    ]
    expected = {
        "unseen": {
            "v1": {"worklist_unseen": {"k-unseen": True, "k-seen": False}},
            "v2": {"worklist_unseen": {"k-no-panel": None}},
        }
    }
    check = _verify(expected, [], findings)
    assert check.ok, check.failures

    findings[1]["unseen"] = None
    findings[2]["unseen"] = 0
    check = _verify(expected, [], findings)
    assert _failures(check) == [
        "team.v1.unseen.worklist.k-seen",
        "team.v2.unseen.worklist.k-no-panel",
    ]
