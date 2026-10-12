"""Measures the numbers that size the alerts BI.

Reports the two approved data-quality diagnostics from design section 3.7 for one selected
team. Read-only - issues aggregations only, never writes. Safe to point at production ECK.

  uv run python scripts/es_scale_probe.py --team checkout-api
  uv run python scripts/es_scale_probe.py --team acceptance-core --run-at 2026-08-25T18:00:00Z
  ES_URL=https://eck.internal:9200 ES_AUTH=user:pass uv run python scripts/es_scale_probe.py --team X
  uv run python scripts/es_scale_probe.py --all-teams        (sizing only, no ownership scope)

SCOPE. A run of the BI itself never reads more than one team, so this probe defaults to the
same discipline: pass --team and it filters by that team's registry operators exactly as the
pipeline does. --all-teams is available for capacity sizing only, and its output is
explicitly labelled as not being any team's numbers.

PRECISION. Every count here is built from terms + cardinality, and cardinality is
HyperLogLog++ and approximate above its precision threshold. These are SIZING figures. The
pipeline never uses them: it pages every matching row and counts identities exactly, because
a reported metric may not be approximate.

Reported per schema, matching the approved definitions:
  node_name_ratio      distinct (application, object/component, node_name)
                       over distinct (application, object/component),
                       using ONLY rows with a nonempty node_name on BOTH sides
  key_inflation_ratio  distinct (application, key_field)
                       over distinct (application, object/component), over ALL rows
A zero denominator yields null, never zero.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ES = os.environ.get("ES_URL", "http://localhost:9200").rstrip("/")
DAYS = int(os.environ.get("DAYS", "7"))
AUTH = os.environ.get("ES_AUTH")
PRECISION = 40000

REPO_ROOT = Path(__file__).resolve().parents[1]


def _search(index: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{ES}/{index}/_search",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    if AUTH:
        request.add_header("Authorization", "Basic " + base64.b64encode(AUTH.encode()).decode())
    with urllib.request.urlopen(request) as response:
        result: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    return result


def _pair_agg(field: str, app_buckets: int) -> dict[str, Any]:
    """Distinct (application, X) pairs without scripting.

    Bucket by application, count distinct X inside each bucket, sum the buckets. Exact on
    the pairing, approximate only within each application's sketch.
    """
    return {
        "terms": {"field": "application", "size": app_buckets},
        "aggs": {"d": {"cardinality": {"field": field, "precision_threshold": PRECISION}}},
    }


def _sum_pairs(agg: dict[str, Any]) -> int:
    return sum(bucket["d"]["value"] for bucket in agg["buckets"])


def _truncated(agg: dict[str, Any]) -> bool:
    return (agg.get("sum_other_doc_count") or 0) > 0


def _ratio(numerator: int, denominator: int) -> float | None:
    """A zero denominator means "no eligible rows".

    That is a different statement from a ratio of zero and must never be printed as one.
    """
    return None if denominator == 0 else numerator / denominator


def _show(value: float | None, digits: int = 3) -> str:
    return "null (no eligible rows)" if value is None else f"{value:.{digits}f}"


def _thousands(value: int) -> str:
    return f"{value:,}"


def probe(
    index: str,
    component_field: str,
    team_operators: list[str],
    *,
    team: str | None,
    all_teams: bool,
    run_at: str | None,
    window: dict[str, str],
) -> None:
    scope = "ALL TEAMS (sizing only)" if all_teams else f"team {team}"
    when = f"{DAYS}d ending {run_at}" if run_at else f"last {DAYS} days, live clock"
    print(f"\n{'=' * 72}\n{index}  ({when}, {scope})\n{'=' * 72}")

    if not all_teams and not team_operators:
        print("  no configured operators for this schema — a real run skips the query entirely")
        return

    filters: list[dict[str, Any]] = [{"range": {"@timestamp": window}}]
    if not all_teams:
        filters.append({"terms": {"operator": team_operators}})
    query = {"bool": {"filter": filters}}

    head = _search(
        index,
        {
            "size": 0,
            "track_total_hits": True,
            "query": query,
            "aggs": {
                "apps": {"cardinality": {"field": "application", "precision_threshold": PRECISION}}
            },
        },
    )
    rows = head["hits"]["total"]["value"]
    apps = head["aggregations"]["apps"]["value"]
    app_buckets = max(apps * 2, 100)

    if rows == 0:
        print("  no rows in this window")
        return

    # All-rows aggregates: the key-inflation numerator and denominator.
    all_rows = _search(
        index,
        {
            "size": 0,
            "query": query,
            "aggs": {
                "keys": _pair_agg("key_field", app_buckets),
                "scopes": _pair_agg(component_field, app_buckets),
            },
        },
    )
    key_numerator = _sum_pairs(all_rows["aggregations"]["keys"])
    key_denominator = _sum_pairs(all_rows["aggregations"]["scopes"])

    # Node-eligible aggregates: ONLY rows with a nonempty node_name, on BOTH sides. A scope
    # that never supplies a node name must not inflate the denominator.
    node_query = {
        "bool": {
            "filter": [*filters, {"exists": {"field": "node_name"}}],
            "must_not": [{"term": {"node_name": ""}}],
        }
    }
    node_agg = _search(
        index,
        {
            "size": 0,
            "track_total_hits": True,
            "query": node_query,
            "aggs": {
                # distinct (application, component, node_name): bucket by application, then
                # by component, then count node names.
                "by_app": {
                    "terms": {"field": "application", "size": app_buckets},
                    "aggs": {
                        "by_component": {
                            "terms": {"field": component_field, "size": 10000},
                            "aggs": {
                                "nodes": {
                                    "cardinality": {
                                        "field": "node_name",
                                        "precision_threshold": PRECISION,
                                    }
                                }
                            },
                        }
                    },
                },
                "scopes": _pair_agg(component_field, app_buckets),
            },
        },
    )

    node_numerator = 0
    for app_bucket in node_agg["aggregations"]["by_app"]["buckets"]:
        for component_bucket in app_bucket["by_component"]["buckets"]:
            node_numerator += component_bucket["nodes"]["value"]
    node_denominator = _sum_pairs(node_agg["aggregations"]["scopes"])
    node_eligible_rows = node_agg["hits"]["total"]["value"]

    if _truncated(all_rows["aggregations"]["keys"]):
        print(f"  !! terms agg truncated — more than {app_buckets} applications. Counts are LOW.")

    print(f"  rows in window                     : {_thousands(rows)}")
    print(f"  distinct applications              : {_thousands(apps)}")
    print(f"  distinct alerts (application+key)  : {_thousands(key_numerator)}")
    print(f"  rows per distinct alert            : {rows / max(key_numerator, 1):.1f}")
    print("")
    print(
        f"  node_name_ratio                    : {_show(_ratio(node_numerator, node_denominator))}"
    )
    print(f"     numerator  (app,{component_field},node) : {_thousands(node_numerator)}")
    print(
        f"     denominator(app,{component_field})      : {_thousands(node_denominator)}"
        f"   [nonempty-node rows only: {_thousands(node_eligible_rows)}]"
    )
    print(f"  key_inflation_ratio                : {_show(_ratio(key_numerator, key_denominator))}")
    print(f"     numerator  (app,key_field)      : {_thousands(key_numerator)}")
    print(
        f"     denominator(app,{component_field})      : {_thousands(key_denominator)}   [all rows]"
    )
    print("")
    print("  Read together: both high suggests node names drive key inflation; high key")
    print("  inflation with a low node ratio points at other identity fields; a high node")
    print("  ratio with low key inflation means many nodes exist without equivalent key")
    print("  growth. Neither ratio contributes to flagged.")

    # Distinct per day, which is how every distinct figure is published.
    daily = _search(
        index,
        {
            "size": 0,
            "query": query,
            "aggs": {
                "per_day": {
                    "date_histogram": {
                        "field": "@timestamp",
                        "calendar_interval": "day",
                        "min_doc_count": 1,
                    },
                    "aggs": {"by_app": _pair_agg("key_field", app_buckets)},
                }
            },
        },
    )
    buckets = daily["aggregations"]["per_day"]["buckets"]
    if buckets:
        sum_days = sum(_sum_pairs(bucket["by_app"]) for bucket in buckets)
        print("")
        print(
            f"  sum of daily distincts             : {_thousands(sum_days)}"
            f" over {len(buckets)} day(s) with data"
        )
        print(
            f"  distinct alerts per day            : {sum_days / 7:.2f}"
            "   [sum / 7, the published rate]"
        )
        print(
            f"  distinct over the whole window     : {_thousands(key_numerator)}"
            "   [internal dedup only, never published]"
        )
        print(f"  key reuse ratio                    : {sum_days / max(key_numerator, 1):.1f}x")
        print(
            "     ~1x -> keys are nearly all new each day, so a cross-run verdict cache"
            " rarely hits."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="es_scale_probe.py", description="Size the alerts BI from Elasticsearch aggregations."
    )
    parser.add_argument("--team", help="scope to one registry team, exactly as a real run does")
    parser.add_argument(
        "--all-teams",
        action="store_true",
        help="company-wide sizing only; not any team's reported numbers",
    )
    # --run-at freezes the window end, matching how a real run captures run_at once. Without
    # it the probe uses a live clock, which finds nothing in a fixed-clock mock dataset.
    parser.add_argument("--run-at", help="freeze the window end at this ISO 8601 instant")
    options = parser.parse_args()

    if not options.team and not options.all_teams:
        parser.print_usage(sys.stderr)
        print("  --team <team_id> or --all-teams is required", file=sys.stderr)
        raise SystemExit(2)

    window: dict[str, str]
    if options.run_at:
        try:
            end = datetime.fromisoformat(options.run_at.replace("Z", "+00:00"))
        except ValueError:
            print(f"--run-at {options.run_at} is not a valid ISO 8601 instant", file=sys.stderr)
            raise SystemExit(2) from None
        window = {
            "gte": (end - timedelta(days=DAYS)).isoformat().replace("+00:00", "Z"),
            "lt": end.isoformat().replace("+00:00", "Z"),
        }
    else:
        window = {"gte": f"now-{DAYS}d", "lte": "now"}

    operators: dict[str, list[str]] = {"v1": [], "v2": []}
    if options.team:
        registry = json.loads((REPO_ROOT / "config" / "teams.json").read_text(encoding="utf-8"))
        entry = next((t for t in registry["teams"] if t["team_id"] == options.team), None)
        if entry is None:
            print(f'team "{options.team}" is not in config/teams.json', file=sys.stderr)
            raise SystemExit(2)
        operators = {
            "v1": entry.get("v1_operators") or [],
            "v2": [entry["v2_operator"]] if entry.get("v2_operator") else [],
        }

    probe(
        "appchi-v1",
        "object",
        operators["v1"],
        team=options.team,
        all_teams=options.all_teams,
        run_at=options.run_at,
        window=window,
    )
    probe(
        "appchi-v2",
        "component",
        operators["v2"],
        team=options.team,
        all_teams=options.all_teams,
        run_at=options.run_at,
        window=window,
    )

    print(
        "\nEvery figure above is APPROXIMATE: cardinality is HyperLogLog++ above"
        f" {_thousands(PRECISION)}"
    )
    print("per bucket. This probe sizes the problem; it is not the measurement contract.")
    print("The pipeline pages every matching row and counts identities exactly.")


if __name__ == "__main__":
    main()
