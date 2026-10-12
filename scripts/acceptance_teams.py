"""Acceptance fixture teams for the alerts BI MVP.

These are NOT a second fixture system: they are team definitions consumed by
``generate_mock_alerts.py`` alongside the seven realistic teams, and they load into the
same ``appchi-v1`` / ``appchi-v2`` indices. They are separated into this file only because
they are authored to a different standard - every row is pinned to an exact timestamp and
an exact expected outcome, so ``test/fixtures/expected-results.json`` can be computed by
hand from these definitions.

The acceptance window is the exact 168 hours ending at the generator's fixed clock:
  run_at       2026-08-25T18:00:00Z
  window_start 2026-08-18T18:00:00Z (inclusive)
  window_end   2026-08-25T18:00:00Z (exclusive)

Every acceptance team except ``acceptance-fire-patterns`` puts its rows on two instants,
DAY1 = 2026-08-23T12:00Z and DAY2 = 2026-08-24T12:00Z, so both single-date and multi-date
allocation are exercised on two full UTC days well inside the window.

Why those two days: these instants were 2026-08-20 and 2026-08-21 until R6 moved to
episodes, when R6 (design section 7.14) measured stuck from the open episode's start to
window_end, and there every single-row Grafana alert would have been stuck. Stuck now
measures the open episode's firing rows from first to last (at least 72 hours), so rows
on DAY1 and DAY2, 24 hours apart at most, can never be stuck wherever they sit; the days
were kept rather than churn every count and date key again.

``acceptance-fire-patterns`` is the one team that exercises R6 on purpose; its rows run
from 2026-08-20 to 2026-08-25, still entirely inside the window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = ["DAY1", "DAY2", "acceptance_teams"]

#: Every acceptance row outside acceptance-fire-patterns lands on one of these instants.
DAY1 = "2026-08-23T12:00:00.000Z"
DAY2 = "2026-08-24T12:00:00.000Z"

RULE_URL = "https://grafana.internal/d/acc-core-1"
GOOD_V1_MESSAGE = "Checkout error rate above 2% of requests over 5m"
GOOD_V2_MESSAGE = "p99 checkout latency above 900ms over 10m"
GOOD_IMPACT = "Customers see slow or failing checkouts"
GOOD_RUNBOOK = "https://runbooks.internal/acceptance/checkout"


def _v1(obj: str, **overrides: Any) -> dict[str, Any]:
    return {
        "application": "acc-app",
        "obj": obj,
        "node_name": "node-a",
        "message": GOOD_V1_MESSAGE,
        "operatorPick": "acc-core",
        "alert_rule_url": RULE_URL,
        "rowsAt": [DAY1],
        **overrides,
    }


def _v2(obj: str, **overrides: Any) -> dict[str, Any]:
    return {
        "application": "acc-app-v2",
        "obj": obj,
        "message": GOOD_V2_MESSAGE,
        "severity": "high",
        "impact": GOOD_IMPACT,
        "runbook_url": GOOD_RUNBOOK,
        "alert_rule_url": RULE_URL,
        "rowsAt": [DAY1],
        **overrides,
    }


#: acceptance-core - one row per case, so every deterministic rule boundary is countable
#: by eye. Each v1 alert uses a distinct ``obj``, which makes each one a distinct identity
#: under the v1 key (application + object + node_name).
ACCEPTANCE_CORE: dict[str, Any] = {
    "name": "acceptance-core",
    "phase": "acceptance",
    "quality": "mixed",
    "schemas": ["v1", "v2"],
    "v1Operators": ["acc-core"],
    "v2Operator": "acc-core-v2",
    "v1PanelQuery": None,
    "v2PanelQuery": None,
    "v1Defs": [
        # --- clean: no core finding, so these go to the model ---
        _v1("c01-clean", provider="grafana"),
        # R4 does NOT apply to API alerts: no rule URL is not evidence against them.
        _v1("c02-api-no-url", alert_rule_url=None, provider="api"),
        # R7 inclusive boundaries: both valid, neither flagged.
        _v1("c03-r7-equal", timeCreated="equal"),
        _v1("c04-r7-oldest", timeCreated="oldest"),
        # --- one core finding each ---
        _v1("c05-r1", message="Error Occurred"),
        _v1("c06-r2", message="i am alive"),
        _v1("Unknown"),
        _v1("c08-r4", alert_rule_url=None, provider="grafana"),
        _v1("c09-r7-future", timeCreated="future"),
        _v1("c10-r7-stale", timeCreated="stale"),
        # --- multi-row identity spanning two dates: three rows, one identity ---
        # The R1 match is on the DAY2 row only, which proves findings are not projected
        # onto the DAY1 rows that did not match, and that one core finding anywhere in the
        # window still withholds the whole identity from the model.
        # Three firing rows spanning DAY1 to DAY2 (24h, under 72h): not stuck, so no R6.
        _v1("c11-multiday", message="Alert triggered", rowsAt=[DAY2]),
        _v1("c11-multiday", rowsAt=[DAY1, DAY1]),
    ],
    "v2Defs": [
        # completion-ready
        _v2("v01-ready"),
        # R8: missing impact
        _v2("v02-r8", severity="warning", impact=None),
        # R9 on critical: blocks phase-2 completion
        _v2("v03-r9-critical", severity="critical", runbook_url=None),
        # R9 on high: visible, but does NOT reduce readiness
        _v2("v04-r9-high", runbook_url=None),
        # R10: impact restates the technical cause
        _v2("v05-r10", severity="warning", impact="high cpu"),
        # R2 core finding on v2: core rules apply to both schemas
        _v2("v06-r2", severity="warning", message="completed successfully"),
    ],
}


def _batching_team() -> dict[str, Any]:
    """acceptance-batching - the grouping and partition paths.

    401 v2 identities share ONE alert_rule_url, so the group must split into balanced
    partitions of 134/134/133. A separate set of API alerts carries no rule URL and must
    fall back to application grouping without ever merging with the rule-URL group.
    """
    big_rule_url = "https://grafana.internal/d/acc-batch-big"
    v2_defs: list[dict[str, Any]] = []

    for index in range(401):
        suffix = f"{index:04d}"
        v2_defs.append(
            {
                "application": "acc-batch-app",
                "obj": f"big-{suffix}",
                "message": f"Queue depth above threshold on shard {suffix}",
                "severity": "warning",
                "impact": "Processing for this shard falls behind",
                "runbook_url": "https://runbooks.internal/acceptance/batch",
                "alert_rule_url": big_rule_url,
                "key_field": f"acc-batch-big-{suffix}",
                "rowsAt": [DAY1],
            }
        )

    for index in range(3):
        v2_defs.append(
            {
                "application": "acc-batch-api-app",
                "obj": f"api-{index}",
                "message": f"API-sent alert {index}: ingest lag above 5m",
                "severity": "warning",
                "impact": "Ingest results arrive late for this stream",
                "runbook_url": "https://runbooks.internal/acceptance/ingest",
                "alert_rule_url": None,
                "provider": "api",
                "key_field": f"acc-batch-api-{index}",
                "rowsAt": [DAY1],
            }
        )

    return {
        "name": "acceptance-batching",
        "phase": "acceptance",
        "quality": "good",
        "schemas": ["v2"],
        "v2Operator": "acc-batching",
        "v1PanelQuery": None,
        "v2PanelQuery": None,
        "v2Defs": v2_defs,
    }


#: acceptance-suppression - every suppression safety path in one team.
#:
#: The registry gives this team two v1 panels so multi-panel unanimity is exercised: a row
#: hidden by both is suppressed, a row hidden by only one is not.
ACCEPTANCE_SUPPRESSION: dict[str, Any] = {
    "name": "acceptance-suppression",
    "phase": "acceptance",
    "quality": "mixed",
    "schemas": ["v1"],
    "v1Operators": ["acc-suppression"],
    "v1PanelQuery": None,
    "v2PanelQuery": None,
    "v1Defs": [
        # Hidden by BOTH panels -> suppressed (R5).
        {
            "application": "acc-sup-app",
            "obj": "s01",
            "node_name": "junk-node",
            "message": GOOD_V1_MESSAGE,
            "operatorPick": "acc-suppression",
            "alert_rule_url": RULE_URL,
            "rowsAt": [DAY1],
        },
        # Hidden by panel A only (its message matches A's NOT LIKE) -> NOT suppressed.
        {
            "application": "acc-sup-app",
            "obj": "s02",
            "node_name": "real-node-1",
            "message": "canary probe reported a fault",
            "operatorPick": "acc-suppression",
            "alert_rule_url": RULE_URL,
            "rowsAt": [DAY1],
        },
        # Named only inside panel B's OR-nested leaf -> unmeasured, never suppressed.
        {
            "application": "acc-sup-app",
            "obj": "s03",
            "node_name": "or-nested-node",
            "message": GOOD_V1_MESSAGE,
            "operatorPick": "acc-suppression",
            "alert_rule_url": RULE_URL,
            "rowsAt": [DAY1],
        },
        # Named only by an unresolved query variable -> unmeasured, never suppressed.
        {
            "application": "acc-sup-app",
            "obj": "s04",
            "node_name": "query-var-node",
            "message": GOOD_V1_MESSAGE,
            "operatorPick": "acc-suppression",
            "alert_rule_url": RULE_URL,
            "rowsAt": [DAY1],
        },
        # Visible in both panels.
        {
            "application": "acc-sup-app",
            "obj": "s05",
            "node_name": "real-node-2",
            "message": GOOD_V1_MESSAGE,
            "operatorPick": "acc-suppression",
            "alert_rule_url": RULE_URL,
            "rowsAt": [DAY1],
        },
        {
            "application": "acc-sup-app",
            "obj": "s06",
            "node_name": "real-node-3",
            "message": GOOD_V1_MESSAGE,
            "operatorPick": "acc-suppression",
            "alert_rule_url": RULE_URL,
            "rowsAt": [DAY1],
        },
    ],
}


#: acceptance-blast-radius - a single panel whose exclusion list reaches more than half the
#: team's owned rows, which the guard must refuse to apply.
#:
#: 3 of 5 rows (60%) are named by the exclusion, so the leaf becomes unmeasured and NO row
#: is suppressed.
ACCEPTANCE_BLAST_RADIUS: dict[str, Any] = {
    "name": "acceptance-blast-radius",
    "phase": "acceptance",
    "quality": "mixed",
    "schemas": ["v1"],
    "v1Operators": ["acc-blast"],
    "v1PanelQuery": None,
    "v2PanelQuery": None,
    "v1Defs": [
        {
            "application": "acc-blast-app",
            "obj": obj,
            "node_name": f"blast-node-{index + 1}",
            "message": GOOD_V1_MESSAGE,
            "operatorPick": "acc-blast",
            "alert_rule_url": RULE_URL,
            "rowsAt": [DAY1],
        }
        for index, obj in enumerate(["b01", "b02", "b03", "b04", "b05"])
    ],
}


FIVE_MINUTES = timedelta(minutes=5)
FIRE_URL = "https://grafana.internal/d/acc-fire-1"
FIRE_V2_URL = "https://grafana.internal/d/acc-fire-v2"


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _every(start: str, step: timedelta, count: int) -> list[str]:
    """``count`` instants from ``start`` (inclusive), ``step`` apart."""
    first = _at(start)
    return [_iso(first + index * step) for index in range(count)]


def _fire(obj: str, rows_at: list[str], **overrides: Any) -> dict[str, Any]:
    return {
        "application": "acc-fire-app",
        "obj": obj,
        "node_name": "node-f",
        "message": GOOD_V1_MESSAGE,
        "operatorPick": "acc-fire",
        "alert_rule_url": FIRE_URL,
        "provider": "grafana",
        "rowsAt": rows_at,
        **overrides,
    }


def _clear(obj: str, rows_at: list[str]) -> dict[str, Any]:
    """v1 clear rows: severity ``clear`` under the same application/object/node_name.

    The v1 key does not include severity, so these share the firing rows' identity.
    """
    return _fire(obj, rows_at, severity="clear")


def _fire_api(obj: str, rows_at: list[str], **overrides: Any) -> dict[str, Any]:
    # API alerts carry no rule URL (R4 is Grafana-only).
    return _fire(
        obj,
        rows_at,
        application="acc-fire-api",
        alert_rule_url=None,
        provider="api",
        **overrides,
    )


def _fire_v2(obj: str, key_field: str, rows_at: list[str], **overrides: Any) -> dict[str, Any]:
    # `resolves` marks the def's LAST row resolved. The v2 key excludes status, so several
    # defs with the same fields are one identity; the explicit key_field just makes that
    # identity readable in the oracle.
    return {
        "application": "acc-fire-app-v2",
        "obj": obj,
        "message": GOOD_V2_MESSAGE,
        "severity": "high",
        "impact": GOOD_IMPACT,
        "runbook_url": GOOD_RUNBOOK,
        "alert_rule_url": FIRE_V2_URL,
        "provider": "grafana",
        "key_field": key_field,
        "rowsAt": rows_at,
        **overrides,
    }


def _fire_v2_episode(fired: str, resolved: str) -> dict[str, Any]:
    """One fire -> resolve cycle of the v2 flapping identity."""
    return _fire_v2("f-v2-flap", "acc-fire-v2-flap", [fired, resolved], resolves=True)


#: acceptance-fire-patterns - R6 boundaries judged by firing EPISODES (design section 7.14).
#:
#: An episode is a run of consecutive firing rows closed by a clear; firing rows after the
#: last clear form the open episode. flapping: >= 3 fire -> clear cycles in a rolling 24h,
#: any provider. spamming: API (non-Grafana) only, n * 24h >= 24 * span and span >= 6h
#: (span = last - first, nothing added); Grafana writes a row per evaluation, so repeated
#: Grafana rows are never spamming. stuck: Grafana whose last row is firing and whose open
#: episode's firing rows span >= 72h (last firing row - open_since, inclusive). It is never
#: measured to window_end: Grafana writes a row on every evaluation while an alert fires, so
#: silence after the last row means it stopped firing, and one row spans 0. Priority
#: flapping -> spamming -> stuck. max_episode_firing_rows is stored as a diagnostic only.
#:
#: One identity per case; every v1 case is a distinct ``obj``. Every Grafana case that
#: should NOT be stuck either ends on a clear or has open-episode rows spanning under 72h.
ACCEPTANCE_FIRE_PATTERNS: dict[str, Any] = {
    "name": "acceptance-fire-patterns",
    "phase": "acceptance",
    "quality": "mixed",
    "schemas": ["v1", "v2"],
    "v1Operators": ["acc-fire"],
    "v2Operator": "acc-fire-v2",
    "v1PanelQuery": None,
    "v2PanelQuery": None,
    "v1Defs": [
        # (a) stuck, inclusive: one row per 12h evaluation from 08-21 12:00 to 08-24 12:00
        #     (7 rows), never cleared. The open episode's rows span exactly 72h.
        _fire("f-a-stuck-72h", _every("2026-08-21T12:00:00.000Z", timedelta(hours=12), 7)),
        # (b) none: 12-hourly from 08-21 12:01 (6 rows to 08-24 00:01), then a last row at
        #     08-24 12:00 (7 rows), never cleared. The rows span 71h59m.
        _fire(
            "f-b-open-71h59m",
            [
                *_every("2026-08-21T12:01:00.000Z", timedelta(hours=12), 6),
                "2026-08-24T12:00:00.000Z",
            ],
        ),
        # (c) none: F, F, C - one episode of two firing rows, closed.
        _fire("f-c-ffc", ["2026-08-24T01:00:00.000Z", "2026-08-24T01:05:00.000Z"]),
        _clear("f-c-ffc", ["2026-08-24T01:10:00.000Z"]),
        # (d) none: F, F, F, C - three firing rows (evaluations) in one closed episode.
        #     Repeated Grafana rows are not spamming.
        _fire(
            "f-d-fffc",
            ["2026-08-24T02:00:00.000Z", "2026-08-24T02:05:00.000Z", "2026-08-24T02:10:00.000Z"],
        ),
        _clear("f-d-fffc", ["2026-08-24T02:15:00.000Z"]),
        # (e) none: F, C, F, F, C - the largest episode has two firing rows.
        _fire(
            "f-e-fcffc",
            ["2026-08-24T03:00:00.000Z", "2026-08-24T03:10:00.000Z", "2026-08-24T03:15:00.000Z"],
        ),
        _clear("f-e-fcffc", ["2026-08-24T03:05:00.000Z", "2026-08-24T03:20:00.000Z"]),
        # (f) stuck: an open episode of 8 firing rows, one per 12h evaluation from 08-21
        #     00:00 to 08-24 12:00, spanning 84h. Many rows in the open episode do not make it
        #     spamming.
        _fire("f-f-open-stuck", _every("2026-08-21T00:00:00.000Z", timedelta(hours=12), 8)),
        # (g) flapping: three single-row episodes on 08-24, clears 00:05, 10:05 and 20:05
        #     (20h apart, inside one rolling 24h).
        _fire(
            "f-g-flap",
            ["2026-08-24T00:00:00.000Z", "2026-08-24T10:00:00.000Z", "2026-08-24T20:00:00.000Z"],
        ),
        _clear(
            "f-g-flap",
            ["2026-08-24T00:05:00.000Z", "2026-08-24T10:05:00.000Z", "2026-08-24T20:05:00.000Z"],
        ),
        # (h) flapping over spamming, API: three F, F, F, C episodes on 08-23 (clears 01:30,
        #     04:30 and 08:00). 12 rows over a span of 8h: 12 x 24h >= 24 x 8h, so it is also
        #     spamming; flapping has priority.
        _fire_api(
            "f-h-api-flap-spam",
            [
                *_every("2026-08-23T00:00:00.000Z", timedelta(minutes=30), 3),
                *_every("2026-08-23T03:00:00.000Z", timedelta(minutes=30), 3),
                *_every("2026-08-23T06:00:00.000Z", timedelta(minutes=30), 3),
            ],
        ),
        _fire_api(
            "f-h-api-flap-spam",
            ["2026-08-23T01:30:00.000Z", "2026-08-23T04:30:00.000Z", "2026-08-23T08:00:00.000Z"],
            severity="clear",
        ),
        # (i) none: three cycles with clears 26h apart first to last (08-22 18:05, 08-23
        #     07:05, 08-23 20:05), so no rolling 24h holds more than two. Ends on a clear.
        _fire(
            "f-i-spread",
            ["2026-08-22T18:00:00.000Z", "2026-08-23T07:00:00.000Z", "2026-08-23T20:00:00.000Z"],
        ),
        _clear(
            "f-i-spread",
            ["2026-08-22T18:05:00.000Z", "2026-08-23T07:05:00.000Z", "2026-08-23T20:05:00.000Z"],
        ),
        # (j) spamming: API, 24 rows over exactly 24h - hourly 08-22 00:00..22:00, then
        #     08-23 00:00. 24 x 24h >= 24 x 24h, span >= 6h.
        _fire_api(
            "f-j-api24",
            [
                *_every("2026-08-22T00:00:00.000Z", timedelta(hours=1), 23),
                "2026-08-23T00:00:00.000Z",
            ],
        ),
        # (k) none: API, 23 rows over the same 24h - hourly 08-22 00:00..21:00, then 08-23
        #     00:00. 23 x 24h < 24 x 24h.
        _fire_api(
            "f-k-api23",
            [
                *_every("2026-08-22T00:00:00.000Z", timedelta(hours=1), 22),
                "2026-08-23T00:00:00.000Z",
            ],
        ),
        # (l) none: API, 30 rows 10 minutes apart from 08-24 00:00 - span 4h50m < 6h.
        _fire_api("f-l-api30-short", _every("2026-08-24T00:00:00.000Z", timedelta(minutes=10), 30)),
        # (m) none: API firing for 100h with no clear - 5 rows 25h apart from 08-20 00:00.
        #     Stuck is Grafana-only, and 5 x 24h < 24 x 100h.
        _fire_api("f-m-api-100h", _every("2026-08-20T00:00:00.000Z", timedelta(hours=25), 5)),
        # (p) none: ONE firing row 100h before window_end (08-21 14:00), then silence. Grafana
        #     writes a row on every evaluation while firing, so silence means it stopped; one
        #     row spans 0h. Measuring to window_end instead would call it stuck (review C1).
        _fire("f-p-stale-100h", ["2026-08-21T14:00:00.000Z"]),
    ],
    "v2Defs": [
        # (n) v2 flapping with per-row `resolved`: one firing row on 08-21, then resolved at
        #     08-23 02:00, firing 06:00, resolved 08:00, firing 12:00, resolved 14:00 - three
        #     clears inside 12h. Ends resolved, so it is not stuck.
        _fire_v2_episode("2026-08-21T12:00:00.000Z", "2026-08-23T02:00:00.000Z"),
        _fire_v2_episode("2026-08-23T06:00:00.000Z", "2026-08-23T08:00:00.000Z"),
        _fire_v2_episode("2026-08-23T12:00:00.000Z", "2026-08-23T14:00:00.000Z"),
        # (o) v2 stuck: one row per 12-hour evaluation from 08-21 00:00 through 08-25 12:00 -
        #     10 rows, never resolved. The open episode's rows span 108h.
        _fire_v2(
            "f-v2-stuck",
            "acc-fire-v2-stuck",
            _every("2026-08-21T00:00:00.000Z", timedelta(hours=12), 10),
        ),
    ],
}


UNSEEN_URL = "https://grafana.internal/d/acc-unseen-1"
UNSEEN_V2_URL = "https://grafana.internal/d/acc-unseen-v2"


def _unseen_v1(obj: str, application: str, node_name: str, rows_at: list[str]) -> dict[str, Any]:
    return {
        "application": application,
        "obj": obj,
        "node_name": node_name,
        "message": GOOD_V1_MESSAGE,
        "operatorPick": "acc-unseen",
        "alert_rule_url": UNSEEN_URL,
        "provider": "grafana",
        "rowsAt": rows_at,
    }


def _unseen_v2(obj: str) -> dict[str, Any]:
    return {
        **_v2(obj),
        "application": "acc-unseen-app-v2",
        "alert_rule_url": UNSEEN_V2_URL,
        "key_field": f"acc-unseen-{obj}",
    }


#: acceptance-unseen - the ``unseen`` visibility measure (team summary spec section 6).
#:
#: The registry gives this team ONE v1 panel and no v2 panel:
#:   WHERE operator = 'acc-unseen' AND application = 'shown-app'
#:     AND (node_name = 'n1' OR severity = 'critical') AND node_name != 'junk'
#: ``application = 'shown-app'`` is the identity leaf that hides rows; the OR-nested
#: ``node_name = 'n1'`` is unmeasured and never hides; ``node_name != 'junk'`` suppresses.
ACCEPTANCE_UNSEEN: dict[str, Any] = {
    "name": "acceptance-unseen",
    "phase": "acceptance",
    "quality": "mixed",
    "schemas": ["v1", "v2"],
    "v1Operators": ["acc-unseen"],
    "v2Operator": "acc-unseen-v2",
    "v1PanelQuery": None,
    "v2PanelQuery": None,
    "v1Defs": [
        # Outside the panel's application narrowing -> unseen, on both dates.
        _unseen_v1("u01-other", "other-app", "n1", [DAY1, DAY2]),
        # Inside the narrowing -> seen.
        _unseen_v1("u02-shown", "shown-app", "n1", [DAY1, DAY2]),
        # Excluded by node_name != 'junk' -> suppressed (R5), not unseen.
        _unseen_v1("u03-junk", "shown-app", "junk", [DAY1]),
        # Fails only the OR-nested node_name = 'n1' leaf, which is unmeasured -> seen.
        _unseen_v1("u04-or", "shown-app", "n2", [DAY1]),
        # Outside the narrowing AND suppressed -> counted only as suppressed.
        _unseen_v1("u05-other-junk", "other-app", "junk", [DAY1]),
    ],
    # No v2 panel, so v2 `unseen` is NULL rather than 0.
    "v2Defs": [_unseen_v2("uv01"), _unseen_v2("uv02")],
}


def acceptance_teams() -> list[dict[str, Any]]:
    """Every acceptance team, appended after the realistic ones.

    New teams go at the END: nothing here draws from the seeded RNG (every def pins its
    operator and its timestamps), but appending keeps that guarantee independent of it.
    """
    return [
        ACCEPTANCE_CORE,
        _batching_team(),
        ACCEPTANCE_SUPPRESSION,
        ACCEPTANCE_BLAST_RADIUS,
        ACCEPTANCE_FIRE_PATTERNS,
        ACCEPTANCE_UNSEEN,
    ]
