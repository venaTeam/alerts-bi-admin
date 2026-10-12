"""The team summary's pure insights: estimate, aggregations and key findings."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from alerts_bi_shared.catalogs import CORE_RULE_IDS
from alerts_bi_shared.insights.aggregate import (
    biggest,
    by_application,
    fire_rows,
    primary_rule_counts,
)
from alerts_bi_shared.insights.estimate import estimate, v1_rule_key
from alerts_bi_shared.insights.findings import key_findings
from alerts_bi_shared.insights.model import (
    AlertRow,
    RuleTotal,
    SchemaTotals,
    SummaryInputs,
    WeekRules,
)
from alerts_bi_shared.insights.summary import summarize

T0 = datetime(2026, 9, 7, tzinfo=UTC)
FORBIDDEN = ("per day", "run_id", "registry", "ruleset", "prompt", "model version")


def alert(**kw: Any) -> AlertRow:
    base: dict[str, Any] = {
        "schema": "v1",
        "application": "app",
        "key_field": "k",
        "message": "m",
        "severity": "error",
        "provider": "grafana",
        "alert_rule_url": None,
        "component": "c",
        "node_name": None,
        "row_count": 1,
        "first_seen": T0,
        "last_seen": T0,
        "quality_state": "assessed_good",
        "core_rule_ids": (),
        "readiness_rule_ids": (),
        "llm_principle_id": None,
        "llm_confidence": None,
        "clear_count": 0,
        "max_clear_cycles_24h": 0,
        "fire_pattern": None,
        "unseen": None,
    }
    base.update(kw)
    return AlertRow(**base)


def totals(schema: str, **kw: Any) -> SchemaTotals:
    base: dict[str, Any] = {
        "schema": schema,
        "events": 0,
        "distinct_alerts": 0,
        "distinct_per_day": None,
        "rule_flagged_events": 0,
        "rule_flagged_alerts": 0,
        "suppressed": 0,
        "unseen": None,
        "unseen_alerts": None,
        "states": {},
        "readiness_gaps": 0,
    }
    base.update(kw)
    return SchemaTotals(**base)


def inputs(**kw: Any) -> SummaryInputs:
    base: dict[str, Any] = {
        "surface": "portal",
        "team_id": "t",
        "display_name": "T",
        "window_start": T0 - timedelta(days=7),
        "window_end": T0,
        "phase": "phase_1",
        "phase2_readiness_pct": None,
        "schemas": {"v1": totals("v1"), "v2": totals("v2")},
        "rules": (),
        "alerts": (),
        "published": True,
        "history": (),
        "v1_rule_effort_days": None,
    }
    base.update(kw)
    return SummaryInputs(**base)


def week(n: int, rules: set[str], basis_changed: bool = False) -> WeekRules:
    return WeekRules(T0 + timedelta(days=7 * n), frozenset(rules), basis_changed)


# --- v1_rule_key ---------------------------------------------------------


def test_rule_key_uses_url_then_application() -> None:
    assert v1_rule_key(alert(alert_rule_url=" http://g/1 ")) == "url:http://g/1"
    assert v1_rule_key(alert(alert_rule_url=None, application="pay")) == "app:pay"
    assert v1_rule_key(alert(alert_rule_url="   ", application="pay")) == "app:pay"


# --- estimate ------------------------------------------------------------


def test_estimate_without_v1_alerts_has_nothing_left() -> None:
    est = estimate(inputs(alerts=(alert(schema="v2"),)))
    assert est.rules_left == 0
    assert est.effort_days == 0
    assert est.effort_weeks == 0


def test_estimate_not_published() -> None:
    alerts = (alert(key_field="a", alert_rule_url="u1"), alert(key_field="b", application="x"))
    est = estimate(inputs(alerts=alerts, published=False, history=()))
    assert est.no_estimate_reason == (
        "This week is not published, so there is no published history to measure a pace from."
    )
    assert est.rules_left == 2
    assert est.effort_days == 1.0
    assert est.effort_weeks == 0.2
    assert not est.effort_is_override
    assert est.projected_week_end is None


def test_estimate_one_week_of_history() -> None:
    est = estimate(inputs(history=(week(0, {"a"}),)))
    assert est.no_estimate_reason == (
        "Needs at least 2 earlier published weeks back to back; none was published before "
        "this week."
    )
    assert est.lookback_weeks == 0


def test_estimate_one_earlier_week_of_history() -> None:
    est = estimate(inputs(history=(week(0, {"a"}), week(1, {"a"}))))
    assert est.no_estimate_reason == (
        "Needs at least 2 earlier published weeks back to back; only 1 was published before "
        "this week."
    )


def test_estimate_gap_stops_the_lookback() -> None:
    history = (week(0, {"a", "b"}), week(1, {"a"}), week(3, {"a"}))
    est = estimate(inputs(history=history))
    assert est.lookback_weeks == 0
    # Two published weeks exist: the reason names the gap, never "found 0".
    assert est.no_estimate_reason == (
        "Needs at least 2 earlier published weeks back to back; a week before was not "
        "published, so none counted."
    )


def test_estimate_gap_after_one_week_counts_that_week() -> None:
    history = (week(0, {"a", "b"}), week(2, {"a"}), week(3, {"a"}))
    est = estimate(inputs(history=history))
    assert est.lookback_weeks == 1
    assert est.no_estimate_reason is not None
    assert "was not published, so only 1 counted." in est.no_estimate_reason


def test_estimate_basis_change_on_selected_week() -> None:
    history = (week(0, {"a", "b"}), week(1, {"a", "b"}), week(2, {"a"}, basis_changed=True))
    est = estimate(inputs(history=history))
    assert est.lookback_weeks == 0
    assert est.no_estimate_reason == (
        "Needs at least 2 earlier published weeks measured the same way; this team's alert "
        "sources, dashboards or the rules changed this week, so none counted."
    )


def test_estimate_basis_change_one_week_back_counts_one_week() -> None:
    history = (week(0, {"a", "b"}), week(1, {"a", "b"}, basis_changed=True), week(2, {"a"}))
    est = estimate(inputs(history=history))
    assert est.lookback_weeks == 1
    assert est.no_estimate_reason is not None
    assert "changed 1 week earlier, so only 1 counted." in est.no_estimate_reason


def test_no_estimate_reason_uses_no_word_the_portal_hides() -> None:
    forbidden = ("per day", "run_id", "registry", "ruleset", "prompt", "model version")
    histories = (
        (week(0, {"a"}),),
        (week(0, {"a"}), week(2, {"a"})),
        (week(0, {"a"}), week(1, {"a"}, basis_changed=True)),
    )
    for history in histories:
        reason = estimate(inputs(history=history)).no_estimate_reason or ""
        assert reason and not [word for word in forbidden if word in reason.lower()]


def test_estimate_basis_change_midway_limits_lookback() -> None:
    history = (
        week(0, {"a", "b", "c"}),
        week(1, {"a", "b"}, basis_changed=True),
        week(2, {"a", "b"}),
        week(3, {"a"}),
    )
    est = estimate(inputs(history=history))
    assert est.lookback_weeks == 2
    assert est.retired == 1
    assert est.no_estimate_reason is not None and "Fewer than 2" in est.no_estimate_reason


def test_estimate_retired_one_is_too_few() -> None:
    history = (week(0, {"a", "b"}), week(1, {"a", "b"}), week(2, {"a"}))
    est = estimate(inputs(history=history))
    assert est.retired == 1
    assert est.pace_per_week is None
    assert est.no_estimate_reason == (
        "Fewer than 2 v1 alert rules stopped firing over the last 2 weeks, "
        "too few to measure a pace."
    )


def test_estimate_happy_path() -> None:
    history = (
        week(0, {"a", "b", "c", "d"}),
        week(1, {"a", "b", "c"}),
        week(2, {"a", "b"}),
        week(3, {"a"}),
    )
    est = estimate(inputs(alerts=(alert(alert_rule_url="a"),), history=history))
    assert est.lookback_weeks == 3
    assert est.retired == 3
    assert est.pace_per_week == 1.0
    assert est.rules_left == 1
    assert est.no_estimate_reason is None
    assert est.projected_week_end == (history[-1].week_end + timedelta(days=7)).date()


def test_estimate_lookback_is_capped_at_three() -> None:
    history = tuple(week(i, {"a", "b", "c", "d", "e"} if i < 5 else {"a"}) for i in range(6))
    assert estimate(inputs(history=history)).lookback_weeks == 3


def test_estimate_no_rules_left_projects_the_selected_week() -> None:
    history = (week(0, {"a", "b"}), week(1, {"a", "b"}), week(2, set()))
    est = estimate(inputs(alerts=(), history=history))
    assert est.rules_left == 0
    assert est.projected_week_end == history[-1].week_end.date()
    assert isinstance(est.projected_week_end, date)
    assert est.effort_days == 0


def test_estimate_override() -> None:
    alerts = tuple(alert(key_field=str(i), alert_rule_url=f"u{i}") for i in range(4))
    est = estimate(inputs(alerts=alerts, v1_rule_effort_days=2.0))
    assert est.effort_days == 8.0
    assert est.effort_weeks == 1.6
    assert est.effort_is_override
    assert est.effort_days_per_rule == 2.0


# --- aggregations ----------------------------------------------------------


def test_by_application_counts_and_order() -> None:
    alerts = (
        alert(
            application="b",
            key_field="1",
            row_count=10,
            quality_state="rule_flagged",
            core_rule_ids=("R3", "R1"),
        ),
        alert(application="b", key_field="2", row_count=5, quality_state="llm_flagged"),
        alert(application="b", key_field="3", row_count=1),
        alert(application="a", key_field="1", row_count=100),
        alert(
            application="c",
            key_field="1",
            row_count=15,
            quality_state="rule_flagged",
            core_rule_ids=("R2",),
        ),
        alert(application="b", schema="v2", key_field="9", row_count=1),
    )
    rows = by_application(alerts)
    assert [(r.application, r.schema) for r in rows] == [
        ("b", "v1"),
        ("c", "v1"),
        ("a", "v1"),
        ("b", "v2"),
    ]
    b = rows[0]
    assert (b.alerts, b.rule_flagged_alerts, b.llm_flagged_alerts) == (3, 1, 1)
    assert (b.events, b.rule_flagged_events, b.llm_flagged_events) == (16, 10, 5)
    assert b.rules == ("R1", "R3")


def test_fire_rows_single_row_has_zero_span_and_no_events_rate() -> None:
    (row,) = fire_rows((alert(row_count=1),), T0)
    assert row.span_hours == 0.0
    assert row.events_per_24h is None
    assert row.open_hours is None
    assert row.max_episode_firing_rows == 0


def test_fire_rows_open_hours_span_the_open_episode_and_orders_by_events() -> None:
    api = alert(provider="api", key_field="x", row_count=48, last_seen=T0 + timedelta(hours=24))
    # The open episode's firing rows run from T0 - 30h to the last row at T0 + 10h: 40h. The
    # week ends later, at T0 + 50h, which open_hours must not reach.
    small = alert(
        key_field="y",
        row_count=2,
        max_episode_firing_rows=2,
        first_seen=T0 - timedelta(hours=30),
        last_seen=T0 + timedelta(hours=10),
        open_since=T0 - timedelta(hours=30),
    )
    rows = fire_rows((small, api), T0 + timedelta(hours=50))
    assert [r.alert.key_field for r in rows] == ["x", "y"]
    assert rows[0].events_per_24h == 48.0
    assert rows[0].span_hours == 24.0
    assert rows[0].pattern is None
    assert rows[1].open_hours == 40.0
    assert rows[1].max_episode_firing_rows == 2
    flagged = fire_rows((alert(fire_pattern="stuck"),), T0)
    assert flagged[0].pattern == "stuck"


def test_biggest_tie_break() -> None:
    a = alert(schema="v2", key_field="a", row_count=9)
    b = alert(schema="v1", key_field="z", row_count=9)
    c = alert(schema="v1", key_field="b", row_count=9)
    assert biggest((a, b, c)) is c
    assert biggest(()) is None


# --- key findings ----------------------------------------------------------


def full_inputs() -> SummaryInputs:
    v1_alerts = tuple(
        alert(key_field=f"k{i}", row_count=c, core_rule_ids=("R1", "R4") if i < 2 else ())
        for i, c in enumerate((900, 50, 20, 20, 10))
    )
    alerts = (
        *v1_alerts,
        alert(schema="v2", key_field="c", severity="critical", readiness_rule_ids=("R9",)),
        alert(schema="v2", key_field="d", severity="warning", readiness_rule_ids=("R9",)),
        alert(schema="v2", key_field="e", severity="critical"),
    )
    return inputs(
        schemas={
            "v1": totals(
                "v1", events=1000, suppressed=7, unseen=3, unseen_alerts=1, states={"unassessed": 2}
            ),
            "v2": totals("v2", events=3, suppressed=2, unseen=1, unseen_alerts=1),
        },
        rules=(
            RuleTotal("v1", "R1", 950, 2),
            RuleTotal("v1", "R4", 950, 2),
            RuleTotal("v2", "R3", 1, 1),
        ),
        alerts=alerts,
    )


def test_key_findings_order_and_cap() -> None:
    findings = key_findings(full_inputs())
    assert [f.kind for f in findings] == [
        "largest",
        "unassessed",
        "concentration",
        "hidden",
        "unseen",
    ]
    assert len(findings) == 5


def test_largest_and_co_occurrence() -> None:
    f = key_findings(full_inputs())[0]
    assert f.title == "Biggest problem: generic message"
    assert f.body == "950 v1 events from 2 alerts. The same alerts also have: no rule link."
    assert f.rule_filter == "R1"
    assert f.fix is None


def test_largest_without_co_occurrence() -> None:
    alerts = (alert(core_rule_ids=("R1",), row_count=3),)
    f = key_findings(inputs(alerts=alerts, rules=(RuleTotal("v1", "R1", 3, 1),)))[0]
    assert f.body == "3 v1 events from 1 alert."


def test_unassessed_text() -> None:
    f = key_findings(full_inputs())[1]
    assert f.title == "2 v1 alerts could not be classified"
    assert f.body.startswith("Unassessed should be zero.")


def test_unassessed_is_counted_per_schema_never_summed() -> None:
    both = inputs(
        schemas={
            "v1": totals("v1", states={"unassessed": 1}),
            "v2": totals("v2", states={"unassessed": 2}),
        }
    )
    (f,) = key_findings(both)
    assert f.title == "1 v1 alert and 2 v2 alerts could not be classified"
    assert "3" not in f.title, "v1 and v2 are never added together"


def test_counts_of_one_read_in_the_singular() -> None:
    one = inputs(
        schemas={
            "v1": totals("v1", suppressed=1, unseen=1, unseen_alerts=1),
            "v2": totals("v2"),
        },
        alerts=(alert(core_rule_ids=("R1",), row_count=1),),
        rules=(RuleTotal("v1", "R1", 1, 1),),
    )
    found = {f.kind: f for f in key_findings(one)}
    assert found["largest"].body == "1 v1 event from 1 alert."
    assert found["hidden"].body == "1 v1 event matches a filter in your dashboard."
    assert found["unseen"].body == "1 v1 alert (1 event) is outside every panel's narrowing."


def test_concentration_threshold() -> None:
    f = next(x for x in key_findings(full_inputs()) if x.kind == "concentration")
    assert f.title == "1 of 5 v1 alerts make 90% of the events"
    assert f.body == "They produced 900 of 1,000 v1 events this week."
    # Two alerts, or an even spread, produce nothing.
    two = inputs(
        alerts=(alert(key_field="a", row_count=99), alert(key_field="b")),
        schemas={"v1": totals("v1", events=100), "v2": totals("v2")},
    )
    assert all(x.kind != "concentration" for x in key_findings(two))
    flat = tuple(alert(key_field=str(i), row_count=10) for i in range(5))
    flat_in = inputs(alerts=flat, schemas={"v1": totals("v1", events=50), "v2": totals("v2")})
    assert next(x for x in key_findings(flat_in) if x.kind == "concentration").title == (
        "4 of 5 v1 alerts make 80% of the events"
    )
    one = tuple(alert(key_field=str(i), row_count=10) for i in range(3))
    # k == n would mean no concentration: 3 equal alerts need all 3 for 80%? 2 of 3 = 67%.
    one_in = inputs(alerts=one, schemas={"v1": totals("v1", events=30), "v2": totals("v2")})
    assert all(x.kind != "concentration" for x in key_findings(one_in))


def test_hidden_omitted_when_nothing_suppressed() -> None:
    base = full_inputs()
    clean = replace(base, schemas={"v1": totals("v1", events=1000), "v2": totals("v2")})
    assert all(f.kind != "hidden" for f in key_findings(clean))
    hidden = next(f for f in key_findings(base) if f.kind == "hidden")
    assert hidden.body == "7 v1 events and 2 v2 events match a filter in your dashboard."
    assert hidden.rule_filter == "R5"


def test_unseen_omitted_when_none_and_present_otherwise() -> None:
    base = full_inputs()
    none = replace(base, schemas={"v1": totals("v1"), "v2": totals("v2")})
    assert all(f.kind != "unseen" for f in key_findings(none))
    f = next(x for x in key_findings(base) if x.kind == "unseen")
    assert (
        f.body
        == "1 v1 alert (3 events) and 1 v2 alert (1 event) are outside every panel's narrowing."
    )
    assert f.fix == "Widen a panel to include them, or confirm they are meant to stay out of view."


def test_readiness_critical_count() -> None:
    base = full_inputs()
    only = replace(base, schemas={"v1": totals("v1"), "v2": totals("v2")}, rules=())
    f = next(x for x in key_findings(only) if x.kind == "readiness")
    assert f.title == "2 of 3 v2 alerts are phase-2 ready"
    assert f.body == "1 critical alert has no runbook."
    assert f.fix == "Add impact and an https:// runbook, starting with critical alerts."
    ok = replace(only, alerts=(alert(schema="v2", severity="warning", readiness_rule_ids=("R9",)),))
    f = key_findings(ok)[0]
    assert f.title == "1 of 1 v2 alerts are phase-2 ready"
    assert f.body == "Every critical alert has a runbook."
    r8 = replace(only, alerts=(alert(schema="v2", readiness_rule_ids=("R8",)),))
    assert key_findings(r8)[0].title == "0 of 1 v2 alerts are phase-2 ready"


def test_no_findings_when_nothing_to_say() -> None:
    assert key_findings(inputs()) == ()


def test_generated_strings_are_free_of_internals() -> None:
    base = full_inputs()
    only = replace(base, schemas={"v1": totals("v1"), "v2": totals("v2")})
    for source in (base, only, inputs()):
        for f in key_findings(source):
            for text in (f.title, f.body, f.fix or ""):
                assert not any(word in text.lower() for word in FORBIDDEN), text
    est = estimate(inputs(history=(week(0, {"a"}),)))
    assert est.no_estimate_reason is not None
    assert not any(w in est.no_estimate_reason.lower() for w in FORBIDDEN)


def test_summarize_assembles_everything() -> None:
    source = full_inputs()
    summary = summarize(source)
    assert summary.inputs is source
    assert summary.key_findings == key_findings(source)
    assert summary.by_application == by_application(source.alerts)
    assert summary.fire == fire_rows(source.alerts, source.window_end)
    assert summary.biggest == biggest(source.alerts)
    assert summary.estimate == estimate(source)


def test_unseen_lists_one_schema_only() -> None:
    one = inputs(
        schemas={
            "v1": totals("v1", unseen=5, unseen_alerts=2),
            "v2": totals("v2", unseen=0, unseen_alerts=0),
        }
    )
    (f,) = key_findings(one)
    assert f.body == "2 v1 alerts (5 events) are outside every panel's narrowing."


def test_concentration_exactly_at_80_percent() -> None:
    alerts = tuple(alert(key_field=str(i), row_count=c) for i, c in enumerate((40, 40, 10, 10)))
    src = inputs(alerts=alerts, schemas={"v1": totals("v1", events=100), "v2": totals("v2")})
    f = next(x for x in key_findings(src) if x.kind == "concentration")
    assert f.title == "2 of 4 v1 alerts make 80% of the events"


# ------------------------------------------------------------------ primary rule (donut)


def _flagged(key: str, *rules: str, schema: str = "v1") -> AlertRow:
    return alert(key_field=key, schema=schema, quality_state="rule_flagged", core_rule_ids=rules)


def test_primary_rule_partitions_by_the_first_rule_in_catalogue_order() -> None:
    alerts = (
        _flagged("a", "R4", "R1"),  # stored order does not matter: R1 comes first
        _flagged("b", "R6"),
        _flagged("c", "R1"),
        _flagged("d", "R7", "R5"),
    )
    assert primary_rule_counts(alerts, "v1") == (("R1", 2), ("R5", 1), ("R6", 1))


def test_primary_rule_counts_a_multi_rule_alert_once() -> None:
    counts = primary_rule_counts((_flagged("a", "R1", "R2", "R3", "R4"),), "v1")
    assert counts == (("R1", 1),)
    assert sum(n for _, n in counts) == 1


def test_primary_rule_excludes_everything_not_rule_flagged_and_other_schemas() -> None:
    alerts = (
        _flagged("a", "R2"),
        alert(key_field="b", quality_state="llm_flagged", llm_principle_id="P1"),
        alert(key_field="c", quality_state="assessed_good", readiness_rule_ids=("R8",)),
        _flagged("d", "R3", schema="v2"),
    )
    assert primary_rule_counts(alerts, "v1") == (("R2", 1),)
    assert primary_rule_counts(alerts, "v2") == (("R3", 1),)


def test_primary_rule_omits_zeros_and_follows_the_catalogue() -> None:
    assert primary_rule_counts((), "v1") == ()
    every = tuple(_flagged(rid, rid) for rid in reversed(CORE_RULE_IDS))
    assert [rid for rid, _ in primary_rule_counts(every, "v1")] == list(CORE_RULE_IDS)
    assert CORE_RULE_IDS == ("R1", "R2", "R3", "R4", "R5", "R6", "R7")
