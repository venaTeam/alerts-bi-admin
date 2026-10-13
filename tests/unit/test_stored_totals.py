from alerts_bi_shared.insights.stored import build_facts, schema_totals


def test_empty_selection_has_zero_known_flagged_events_but_unknown_daily_measurements() -> None:
    totals = schema_totals(build_facts([]), [], admin=False)["v1"]
    assert totals.events == totals.distinct_alerts == totals.rule_flagged_events == 0
    assert totals.unseen is None and totals.unseen_alerts is None
    assert totals.distinct_per_day is None


def test_admin_uses_daily_event_counts_and_empty_daily_defaults() -> None:
    facts = build_facts([])
    totals = schema_totals(
        facts,
        [
            {
                "alert_schema": "v1",
                "alerts": 7,
                "distinct_alerts": 1,
                "flagged_by_rule": 2,
                "suppressed": 0,
            }
        ],
        admin=True,
    )
    assert totals["v1"].events == 7 and totals["v1"].rule_flagged_events == 2
    assert totals["v2"].distinct_per_day == totals["v2"].rule_flagged_events == 0
