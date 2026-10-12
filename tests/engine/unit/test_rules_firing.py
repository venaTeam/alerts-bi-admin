"""R6: stuck, spamming and flapping, judged by firing episodes (design 7.14)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from src.engine.domain.normalize import AlertRecord, select_representative
from src.engine.rules.engine import compute_daily_rule_counts, evaluate_rows
from src.engine.rules.firing import FiringFacts, firing_facts, is_clear

from tests.engine.helpers.rows import v1_row, v2_row

T0 = datetime(2026, 8, 18, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _facts(rows: list[AlertRecord], schema: str = "v1") -> FiringFacts:
    return firing_facts(schema, rows, select_representative(rows).provider)


def _v1(times: list[datetime], clears: set[int] | None = None, **kw: Any) -> list[AlertRecord]:
    clears = clears or set()
    return [
        v1_row(**{"@timestamp": _iso(t), "severity": 1 if i in clears else 5, **kw})
        for i, t in enumerate(times)
    ]


def _seq(pattern: str, step: timedelta = HOUR, **kw: Any) -> list[AlertRecord]:
    """``F`` is a firing row and ``C`` a clear row, ``step`` apart from T0."""
    times = [T0 + i * step for i in range(len(pattern))]
    return _v1(times, clears={i for i, c in enumerate(pattern) if c == "C"}, **kw)


def _even(n: int, length: timedelta) -> list[datetime]:
    secs = int(length.total_seconds())
    return [T0 + timedelta(seconds=round(i * secs / (n - 1))) for i in range(n)]


API: dict[str, Any] = {"provider": "api", "alert_rule_url": None}


# ------------------------------------------------------------------------- stuck


def test_open_episode_whose_firing_rows_span_exactly_72h_is_stuck() -> None:
    # One row per 12-hour evaluation; the rows span 72h exactly (inclusive).
    times = [T0 + i * timedelta(hours=12) for i in range(7)]
    facts = _facts(_v1(times))
    assert facts.open_since == T0
    assert facts.open_span == timedelta(hours=72)
    assert facts.pattern == "stuck"


def test_open_episode_whose_firing_rows_span_71h59m_is_not_stuck() -> None:
    times = [T0 + i * timedelta(hours=12) for i in range(6)]
    times.append(T0 + timedelta(hours=71, minutes=59))
    facts = _facts(_v1(times))
    assert facts.open_span == timedelta(hours=71, minutes=59)
    assert facts.pattern is None


def test_one_grafana_firing_row_then_silence_is_never_stuck() -> None:
    # C1: one firing row 100h before the week ends, then nothing. Grafana writes a row on
    # every evaluation, so silence means it stopped firing; one row spans zero.
    facts = _facts(_v1([WINDOW_END - timedelta(hours=100)]))
    assert facts.open_since == WINDOW_END - timedelta(hours=100)
    assert facts.open_span == timedelta(0)
    assert facts.span == timedelta(0)
    assert facts.pattern is None


def test_a_short_burst_long_before_the_week_ends_is_not_stuck() -> None:
    # Three rows in ten minutes, 100h before the end: the rows span 10 minutes.
    start = WINDOW_END - timedelta(hours=100)
    facts = _facts(_v1([start, start + timedelta(minutes=5), start + timedelta(minutes=10)]))
    assert facts.open_span == timedelta(minutes=10)
    assert facts.pattern is None


def test_stuck_does_not_depend_on_how_long_ago_the_last_row_was() -> None:
    # The rows span 72h and end four days before the week ends: still stuck, because the
    # open episode as its rows show it lasted 72h.
    end = WINDOW_END - timedelta(hours=96)
    facts = _facts(_v1([end - timedelta(hours=72), end - timedelta(hours=36), end]))
    assert facts.open_span == timedelta(hours=72)
    assert facts.pattern == "stuck"


def test_stuck_measures_only_the_open_episode_not_earlier_episodes() -> None:
    # 80h of rows overall, but the clear at +60h leaves an open episode spanning 20h.
    times = [T0, T0 + timedelta(hours=59), T0 + timedelta(hours=60), T0 + timedelta(hours=80)]
    facts = _facts(_v1(times, clears={2}))
    assert facts.open_since == T0 + timedelta(hours=80)
    assert facts.open_span == timedelta(0)
    assert facts.pattern is None


def test_a_grafana_alert_that_cleared_is_not_stuck_and_has_no_open_episode() -> None:
    facts = _facts(_v1([T0, T0 + HOUR], clears={1}))
    assert facts.open_since is None
    assert facts.open_span is None
    assert facts.pattern is None


def test_open_since_is_the_first_firing_row_after_the_last_clear() -> None:
    facts = _facts(_seq("FCFF"))
    assert facts.open_since == T0 + 2 * HOUR


def test_a_grafana_alert_that_cleared_then_fired_again_for_72h_is_stuck() -> None:
    # Fire, clear, then firing rows every 12 h spanning exactly 72 h: only the episode
    # after the last clear is measured, and it is long enough.
    reopen = T0 + 2 * HOUR
    times = [T0, T0 + HOUR] + [reopen + i * timedelta(hours=12) for i in range(7)]
    facts = _facts(_v1(times, clears={1}))
    assert facts.open_since == reopen
    assert facts.pattern == "stuck"


def test_api_alert_firing_for_100h_is_never_stuck() -> None:
    facts = _facts(_v1([T0, T0 + timedelta(hours=100)], **API))
    assert facts.open_span == timedelta(hours=100)
    assert facts.pattern is None


# ---------------------------------------------------------------------- spamming


def test_grafana_firing_rows_inside_one_episode_are_never_spamming() -> None:
    # Grafana writes a row on every evaluation, so a row count says nothing about re-sending.
    facts = _facts(_seq("FFFC"))
    assert facts.max_episode_firing_rows == 3
    assert facts.pattern is None


def test_the_longest_of_several_episodes_counts() -> None:
    facts = _facts(_seq("FCFFC"))
    assert facts.max_episode_firing_rows == 2
    assert facts.pattern is None


def test_a_288_row_grafana_episode_spanning_at_least_72h_is_stuck() -> None:
    step = timedelta(minutes=25)
    times = [WINDOW_END - timedelta(hours=120) + i * step for i in range(288)]
    facts = _facts(_v1(times))
    assert facts.open_since == times[0]
    assert facts.open_span == timedelta(minutes=25 * 287)  # 119h35m
    assert facts.max_episode_firing_rows == 288
    assert facts.pattern == "stuck"


def test_a_huge_grafana_row_count_in_an_episode_open_under_72h_is_nothing() -> None:
    times = [WINDOW_END - timedelta(hours=30) + timedelta(seconds=300 * i) for i in range(288)]
    facts = _facts(_v1(times))
    assert facts.max_episode_firing_rows == 288
    assert facts.pattern is None


def test_three_api_rows_at_one_instant_are_not_spamming() -> None:
    assert _facts(_v1([T0, T0, T0], **API)).pattern is None  # span 0 is under 6 h


def test_two_replica_duplicates_at_one_instant_are_tolerated() -> None:
    assert _facts(_v1([T0, T0, T0 + HOUR], clears={2})).pattern is None


def test_api_alert_at_24_rows_over_exactly_24h_is_spamming() -> None:
    facts = _facts(_v1(_even(24, timedelta(hours=24)), **API))
    assert facts.span == timedelta(hours=24)
    assert facts.pattern == "spamming"


def test_api_alert_with_23_rows_is_not_spamming() -> None:
    assert _facts(_v1(_even(23, timedelta(hours=24)), **API)).pattern is None


def test_api_alert_at_exactly_six_hours_can_be_spamming() -> None:
    facts = _facts(_v1(_even(7, timedelta(hours=6)), **API))
    assert facts.span == timedelta(hours=6)
    assert facts.events_per_24h == 28.0
    assert facts.pattern == "spamming"


def test_api_alert_at_5h59m_is_never_spamming() -> None:
    facts = _facts(_v1(_even(30, timedelta(hours=5, minutes=59)), **API))
    assert facts.events_per_24h is None
    assert facts.pattern is None


def test_api_alert_needs_six_hours_of_span() -> None:
    facts = _facts(_v1(_even(30, timedelta(hours=5)), **API))
    assert facts.events_per_24h is None
    assert facts.pattern is None


# ---------------------------------------------------------------------- flapping


def _cycles(clear_hours: list[float]) -> list[AlertRecord]:
    """A fire row one hour before each clear row."""
    times: list[datetime] = []
    clears: set[int] = set()
    for h in clear_hours:
        times.append(T0 + timedelta(hours=h - 1))
        clears.add(len(times))
        times.append(T0 + timedelta(hours=h))
    return _v1(times, clears=clears)


def test_three_cycles_within_24h_is_flapping() -> None:
    facts = _facts(_cycles([2, 8, 20]))
    assert facts.max_clear_cycles_24h == 3
    assert facts.pattern == "flapping"


def test_cycles_spread_over_more_than_24h_are_not_flapping() -> None:
    facts = _facts(_cycles([2, 14, 27]))
    assert facts.max_clear_cycles_24h == 2
    assert facts.pattern is None


def test_flap_window_is_half_open() -> None:
    assert _facts(_cycles([1, 13, 25])).max_clear_cycles_24h == 2


def test_consecutive_clear_rows_are_one_cycle() -> None:
    facts = _facts(_v1([T0, T0 + HOUR, T0 + 2 * HOUR], clears={1, 2}))
    assert facts.clear_count == 2
    assert facts.max_clear_cycles_24h == 1


def test_v2_flapping_reads_status_resolved() -> None:
    rows = [
        v2_row(**{"@timestamp": _iso(T0 + i * HOUR), "status": "resolved" if i % 2 else "firing"})
        for i in range(6)
    ]
    assert is_clear(rows[1]) and not is_clear(rows[0])
    facts = _facts(rows, "v2")
    assert facts.max_clear_cycles_24h == 3
    assert facts.pattern == "flapping"


def test_flapping_outranks_spamming() -> None:
    # An API alert at ~3.6 events per hour over ~6.6 h is spamming on its own.
    step = timedelta(minutes=4)
    spam = _seq("F" * 100, step, **API)
    assert _facts(spam).pattern == "spamming"
    flapping = _seq("FCFCFC" + "F" * 94, step, **API)
    assert _facts(flapping).pattern == "flapping"


# ------------------------------------------------------------------------- facts


def test_input_order_does_not_change_the_facts() -> None:
    rows = _seq("FFCFFFC", timedelta(minutes=20)) + _v1([T0, T0])
    assert _facts(rows) == _facts(list(reversed(rows)))


def test_a_clear_at_the_same_instant_as_a_firing_row_closes_the_episode() -> None:
    at = WINDOW_END - timedelta(hours=96)
    firing = v1_row(**{"@timestamp": _iso(at), "severity": 5})
    # The clear must hash BELOW the firing row, so only the is_clear sort key (not the hash
    # tie-break) can place it after the firing row.
    clear = next(
        row
        for i in range(200)
        if (row := v1_row(**{"@timestamp": _iso(at), "severity": 1, "message": f"m{i}"})).doc_hash
        < firing.doc_hash
    )
    assert clear.doc_hash < firing.doc_hash
    # An earlier firing row makes the would-be open episode span 96h, so without the
    # same-instant clear closing it, this would be stuck.
    earlier = v1_row(**{"@timestamp": _iso(at - timedelta(hours=96)), "severity": 5})
    for rows in ([earlier, firing, clear], [clear, firing, earlier]):
        facts = _facts(rows)
        assert facts.open_since is None
        assert facts.pattern is None


def test_all_clear_rows_have_no_episode() -> None:
    facts = _facts(_seq("CC"))
    assert facts.max_episode_firing_rows == 0
    assert facts.open_since is None
    assert facts.pattern is None


def test_ties_on_timestamp_are_ordered_by_document_hash() -> None:
    t = [T0 + i * HOUR for i in range(3)]
    fire0 = v1_row(**{"@timestamp": _iso(t[0]), "severity": 5})
    clear_mid = v1_row(**{"@timestamp": _iso(t[1]), "severity": 1})
    fire_mid = v1_row(**{"@timestamp": _iso(t[1]), "severity": 5})
    clear_end = v1_row(**{"@timestamp": _iso(t[2]), "severity": 1})
    mid = sorted([clear_mid, fire_mid], key=lambda r: r.doc_hash)
    # Hash order fire-then-clear gives F,F,C,C (1 cycle); clear-then-fire gives F,C,F,C (2).
    expected = 1 if mid[0] is fire_mid else 2
    for rows in (
        [fire0, clear_mid, fire_mid, clear_end],
        [clear_end, fire_mid, clear_mid, fire0],
    ):
        assert _facts(rows).max_clear_cycles_24h == expected


def test_engine_attaches_r6_to_every_row_and_withholds_from_llm() -> None:
    # The rows span 72h and the last one is 10h before the week ends: open_hours is the
    # span of the open episode's firing rows (72), not the time to the week's end (82).
    times = [
        WINDOW_END - timedelta(hours=82),
        WINDOW_END - timedelta(hours=40),
        WINDOW_END - timedelta(hours=10),
    ]
    rows = _v1(times)
    evaluation = evaluate_rows(rows, WINDOW_END)
    (identity,) = evaluation.identities.values()
    assert identity.core_rule_ids == ["R6"]
    assert identity.fire_pattern == "stuck"
    assert identity.clear_count == 0
    assert identity.max_episode_firing_rows == len(rows)
    assert identity.open_since == times[0]
    assert identity.llm_eligible is False
    for item in evaluation.rows:
        (finding,) = item.core_findings
        assert finding.rule_id == "R6"
        assert finding.evidence["pattern"] == "stuck"
        assert finding.evidence["open_hours"] == 72.0
        assert finding.evidence["max_episode_firing_rows"] == len(rows)
    dates = sorted({r.snapshot_date for r in rows})
    counts = [c for c in compute_daily_rule_counts(evaluation.rows, dates) if c.rule_id == "R6"]
    assert {c.snapshot_date for c in counts} == set(dates)
    assert all(c.distinct_count == 1 for c in counts)
