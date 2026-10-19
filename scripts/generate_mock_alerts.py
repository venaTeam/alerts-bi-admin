"""Generates the mock multi-team alert dataset and bulk-loads it into the local mock.

Covers the v1 (Appchi) and v2 (Appchi V2) schemas described in docs/alerts_bi_design.md section
1.3. This is throwaway test-data tooling for the "mock environment first" decision (design
section 6) - not part of the BI pipeline itself.

Usage:
  poetry run python scripts/generate_mock_alerts.py            append another copy of the data
  RESET=1 poetry run python scripts/generate_mock_alerts.py    clean reload (acceptance)
  STATS_ONLY=1 poetry run python scripts/generate_mock_alerts.py   statistics only, no writes

The seven realistic teams' definitions live in ``mock_teams.json``, which was exported
mechanically from the superseded JavaScript generator during the port so that not one
fixture definition was retyped. The acceptance teams live in ``acceptance_teams.py``
because they are hand-authored against exact expected outcomes.

Row cadence: Grafana writes an Elasticsearch row on EVERY rule evaluation while an alert is
firing - every 5 minutes in v1 and every 12 hours in v2 - which is why production carries
about 100 rows per alert per day. The notification repeat interval is a different setting
and is disabled on both schemas (design sections 1.1 and 7.14). A def's ``rowsAt`` pins its
rows exactly; that is how the acceptance teams author an exact firing episode.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha1
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.engine.config import load_dotenv, load_es_config
from src.engine.domain.severity import code_for_name

from scripts._jsrandom import Random
from scripts.acceptance_teams import acceptance_teams

load_dotenv()
ES_CONFIG = load_es_config()
ES_URL = ES_CONFIG.url
NOW = datetime(2026, 8, 25, 18, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

#: Seeded so the dataset is reproducible. The sequence is bit-identical to the JavaScript
#: generator's, so the realistic teams' data is unchanged by the port.
RNG = Random(20260825)

SCRIPT_DIR = Path(__file__).resolve().parent

# ---- evaluation cadence (design doc sections 1.1 and 7.14) ----
# Grafana writes a row per evaluation of a still-firing rule: every 5 MINUTES in v1 and every
# 12 HOURS in v2. The notification repeat interval is disabled; these rows are evaluations,
# not re-sent notifications. A def's (refireCount x intervalHours) authors how long the
# alert was firing; the row count is that duration divided by the schema's evaluation
# cadence. Only Grafana-provider alerts are evaluated this way - API alerts are sent by the
# client at whatever cadence it chooses, so those keep their authored cadence.
V1_EVALUATION_CADENCE = timedelta(minutes=5)
V2_EVALUATION_CADENCE = timedelta(hours=12)

TWENTY_FOUR_HOURS = timedelta(hours=24)

# The R2 and R3 catalogues are deliberately NOT duplicated here: the authoritative lists
# live in src/rules/catalogs.py, and fixture defs that need a catalogue value
# write the literal string.
RULE1_GENERIC = [
    "Error Occurred",
    "Something went wrong",
    "Unable to get data",
    "Alert triggered",
    "Issue detected",
]

V2_KEY_FIELDS = (
    "application",
    "component",
    "severity",
    "impact",
    "runbook_url",
    "environment",
    "site",
    "operator",
    "node_name",
    "network",
    "alert_rule_url",
    "provider",
)


def v1_key_field(application: str, obj: str, node_name: str | None) -> str:
    return f"{application}:{obj}:{node_name or 'n-a'}"


def v2_key_field(doc: dict[str, Any]) -> str:
    """v2 key_field is a hash of every field EXCEPT status, message and the time fields.

    impact, runbook_url and severity ARE in the key, which is why enriching an alert mints
    a new one (design section 3.7).
    """
    joined = "|".join(
        f"{doc.get(field) if doc.get(field) is not None else ''}" for field in V2_KEY_FIELDS
    )
    # sha1 here is a fixture identity function, not a security primitive.
    return sha1(joined.encode("utf-8")).hexdigest()[:16]


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _evaluation_rows(definition: dict[str, Any], cadence: timedelta) -> tuple[int, timedelta]:
    if (definition.get("provider") or "grafana") != "grafana":
        return definition.get("refireCount", 1), timedelta(
            hours=definition.get("intervalHours", 12)
        )
    firing = (definition.get("refireCount", 1) - 1) * timedelta(
        hours=definition.get("intervalHours", 12)
    )
    return int(firing / cadence) + 1, cadence


def _time_created_for(definition: dict[str, Any], timestamp: datetime) -> str | None:
    """Acceptance defs pin ``time_created`` to an exact R7 relationship.

    'equal' and 'oldest' are the two INCLUSIVE boundaries and must not be flagged;
    'future' and 'stale' sit one millisecond outside them and must be.
    """
    mode = definition.get("timeCreated", "valid")
    if mode is None:
        return None
    if mode == "equal":
        return _iso(timestamp)
    if mode == "oldest":
        return _iso(timestamp - TWENTY_FOUR_HOURS)
    if mode == "future":
        return _iso(timestamp + timedelta(milliseconds=1))
    if mode == "stale":
        return _iso(timestamp - TWENTY_FOUR_HOURS - timedelta(milliseconds=1))
    return _iso(timestamp)


def _row_timestamps(definition: dict[str, Any], cadence: timedelta) -> list[datetime]:
    """Explicit timestamps when the def pins them, otherwise the authored cadence."""
    if definition.get("rowsAt"):
        return [
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            for value in definition["rowsAt"]
        ]
    count, step = _evaluation_rows(definition, cadence)
    recency = definition.get("recencyDays")
    end_offset = recency * DAY if recency is not None else RNG.integer(0, 3) * DAY
    last = NOW - end_offset
    return [last - (count - 1 - index) * step for index in range(count)]


def expand_v1(team: dict[str, Any], definition: dict[str, Any]) -> list[dict[str, Any]]:
    operator = definition.get("operatorPick") or RNG.pick(team["v1Operators"])
    timestamps = _row_timestamps(definition, V1_EVALUATION_CADENCE)
    invalid_time = 7 in (definition.get("badRule") or [])
    rows = []

    for index, timestamp in enumerate(timestamps):
        if "timeCreated" in definition:
            time_created = _time_created_for(definition, timestamp)
        elif invalid_time:
            time_created = None if index == len(timestamps) - 1 else _iso(timestamp)
        else:
            time_created = _iso(timestamp)

        rows.append(
            {
                "index": ES_CONFIG.v1_index,
                "doc": {
                    "id": str(uuid.uuid4()),
                    "@timestamp": _iso(timestamp),
                    "application": definition["application"],
                    "object": definition["obj"],
                    "message": definition["message"],
                    # Stored as a number. code_for_name raises on a name outside
                    # the standard, so a typo in a fixture fails the build.
                    "severity": code_for_name(definition.get("severity") or "error"),
                    "operator": operator,
                    "key_field": definition.get("key_field")
                    or v1_key_field(
                        definition["application"], definition["obj"], definition.get("node_name")
                    ),
                    "time_created": time_created,
                    "node_name": definition.get("node_name") or None,
                    "network": definition.get("network") or None,
                    "alert_rule_url": definition.get("alert_rule_url") or None,
                    "provider": definition.get("provider") or "grafana",
                },
            }
        )
    return rows


def expand_v2(team: dict[str, Any], definition: dict[str, Any]) -> list[dict[str, Any]]:
    operator = team["v2Operator"]
    timestamps = _row_timestamps(definition, V2_EVALUATION_CADENCE)
    base = {
        "application": definition["application"],
        "component": definition["obj"],
        "message": definition["message"],
        "severity": code_for_name(definition.get("severity") or "warning"),
        "impact": definition.get("impact"),
        "runbook_url": definition.get("runbook_url"),
        "environment": definition.get("environment") or "production",
        "site": definition.get("site") or None,
        "node_name": definition.get("node_name") or None,
        "network": definition.get("network") or None,
        "alert_rule_url": definition.get("alert_rule_url") or None,
        "provider": definition.get("provider") or "grafana",
    }

    rows = []
    for index, timestamp in enumerate(timestamps):
        status = (
            "resolved" if definition.get("resolves") and index == len(timestamps) - 1 else "firing"
        )
        doc = {**base, "status": status}
        rows.append(
            {
                "index": ES_CONFIG.v2_index,
                "doc": {
                    "id": str(uuid.uuid4()),
                    "@timestamp": _iso(timestamp),
                    **doc,
                    "operator": operator,
                    "key_field": definition.get("key_field")
                    or v2_key_field({**doc, "operator": operator}),
                    "time_created": _iso(timestamp),
                },
            }
        )
    return rows


def review_documents(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand semantic-review cases with the existing fixture builders, without seeding ES.

    Explicit timestamps/operators avoid consuming the ordinary fixture's random stream.
    Stable synthetic source IDs keep the complete documents byte-stable across trials.
    """
    team = {"v1Operators": ["review-evaluation"], "v2Operator": "review-evaluation-v2"}
    documents = []
    for case in cases:
        if ("source" in case) == ("definition" in case):
            raise ValueError(
                "each review case needs exactly one frozen source or fixture definition"
            )
        if "source" in case:
            # Operator-supplied, already selected representative: preserve every field.
            documents.append(deepcopy(case["source"]))
            continue
        definition = {
            **case["definition"],
            "key_field": case["case_id"],
            "rowsAt": ["2026-08-25T12:00:00.000Z"],
            "operatorPick": "review-evaluation",
        }
        expand = expand_v1 if case["schema"] == "v1" else expand_v2
        document = expand(team, definition)[0]["doc"]
        document["id"] = str(
            uuid.uuid5(uuid.NAMESPACE_URL, "alerts-bi-evaluation:" + case["case_id"])
        )
        documents.append(document)
    return documents


# ---- explicit index mappings, so a clean reload is reproducible ----
# Without them the first document decides the mapping dynamically, which makes a reloaded
# index depend on insertion order. `key_field` must be keyword for exact, case-sensitive
# term matching. `operator` mirrors the production v1/v2 mapping - analyzed text with a
# `keyword` subfield - so the reader's exact match on `operator.keyword` is exercised here;
# a query on the analyzed field itself would silently drop mixed-case operators.
_TEXT_WITH_RAW = {"type": "text", "fields": {"raw": {"type": "keyword", "ignore_above": 1024}}}
_TEXT_WITH_KEYWORD = {
    "type": "text",
    "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
}
INDEX_MAPPINGS: dict[str, dict[str, Any]] = {
    ES_CONFIG.v1_index: {
        "@timestamp": {"type": "date"},
        "id": {"type": "keyword"},
        "application": {"type": "keyword"},
        "object": {"type": "keyword"},
        "message": _TEXT_WITH_RAW,
        "severity": {"type": "integer"},
        "operator": _TEXT_WITH_KEYWORD,
        "key_field": {"type": "keyword"},
        "time_created": {"type": "date"},
        "node_name": {"type": "keyword"},
        "network": {"type": "keyword"},
        "alert_rule_url": {"type": "keyword"},
        "provider": {"type": "keyword"},
    },
    ES_CONFIG.v2_index: {
        "@timestamp": {"type": "date"},
        "id": {"type": "keyword"},
        "application": {"type": "keyword"},
        "component": {"type": "keyword"},
        "message": _TEXT_WITH_RAW,
        "severity": {"type": "integer"},
        "status": {"type": "keyword"},
        "impact": _TEXT_WITH_RAW,
        "runbook_url": {"type": "keyword"},
        "environment": {"type": "keyword"},
        "site": {"type": "keyword"},
        "operator": _TEXT_WITH_KEYWORD,
        "key_field": {"type": "keyword"},
        "time_created": {"type": "date"},
        "node_name": {"type": "keyword"},
        "network": {"type": "keyword"},
        "alert_rule_url": {"type": "keyword"},
        "provider": {"type": "keyword"},
    },
}

#: RESET=1 is only allowed against an explicit local mock endpoint, so a mistyped ES_URL
#: cannot delete a real index.
LOCAL_ES = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$", re.IGNORECASE)


def _request(
    method: str, path: str, body: bytes | None = None, content_type: str | None = None
) -> Any:
    request = urllib.request.Request(f"{ES_URL}{path}", data=body, method=method)
    if content_type:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if method == "DELETE" and exc.code == 404:
            return None
        raise
    return json.loads(text) if text else None


def _validate_write_targets() -> None:
    """Validate writer targets without restricting imports of pure fixture helpers."""
    for index in (ES_CONFIG.v1_index, ES_CONFIG.v2_index):
        if index.startswith(("_", "-", "+", ".")) or re.search(r'[\\/*?"<>|,#:]', index):
            raise ValueError("mock ES_V1_INDEX and ES_V2_INDEX must be concrete index names")
    if ES_CONFIG.v1_index == ES_CONFIG.v2_index:
        raise ValueError("mock ES_V1_INDEX and ES_V2_INDEX must be distinct")


def reset_indices() -> None:
    _validate_write_targets()
    if not LOCAL_ES.match(ES_URL):
        raise SystemExit(
            f"refusing to reset indices at {ES_URL}: RESET=1 is only allowed against an "
            "explicit local mock endpoint"
        )
    for index, properties in INDEX_MAPPINGS.items():
        _request("DELETE", f"/{index}")
        body = json.dumps(
            {
                "settings": {"number_of_shards": 1, "number_of_replicas": 0},
                "mappings": {"properties": properties},
            }
        ).encode("utf-8")
        _request("PUT", f"/{index}", body, "application/json")
        print(f"recreated {index} with explicit mappings")


def bulk_load(rows: list[dict[str, Any]]) -> None:
    _validate_write_targets()
    chunk_size = 300
    for start in range(0, len(rows), chunk_size):
        lines = []
        for row in rows[start : start + chunk_size]:
            lines.append(json.dumps({"index": {"_index": row["index"], "_id": row["doc"]["id"]}}))
            lines.append(json.dumps(row["doc"]))
        body = ("\n".join(lines) + "\n").encode("utf-8")
        response = _request("POST", "/_bulk", body, "application/x-ndjson")
        if response and response.get("errors"):
            first = next(
                (item for item in response["items"] if item.get("index", {}).get("error")), None
            )
            print(f"Bulk errors present, first: {json.dumps(first)}", file=sys.stderr)
            raise SystemExit(1)
    for index in INDEX_MAPPINGS:
        _request("POST", f"/{index}/_refresh")
    print(f"Indexed {len(rows)} total rows.")


def main() -> None:
    definitions = json.loads((SCRIPT_DIR / "mock_teams.json").read_bytes())
    teams: list[dict[str, Any]] = list(definitions["teams"])
    unattributed = definitions["unattributed"]

    # Acceptance fixture teams are appended LAST on purpose: expansion consumes the seeded
    # RNG in team order, so adding these at the end leaves every realistic team's generated
    # data byte-stable. Acceptance defs pin their own operator and timestamps, so they
    # consume no RNG draws themselves.
    teams.extend(acceptance_teams())

    rows: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    rule_breakdown: list[dict[str, Any]] = []

    for team in teams:
        v1_rows = v2_rows = v1_distinct = v2_distinct = 0

        for definition in team.get("v1Defs") or []:
            expanded = expand_v1(team, definition)
            rows.extend(expanded)
            v1_rows += len(expanded)
            v1_distinct += 1
        _tally_rules(rule_breakdown, team, "v1", team.get("v1Defs") or [], V1_EVALUATION_CADENCE)

        for definition in team.get("v2Defs") or []:
            expanded = expand_v2(team, definition)
            rows.extend(expanded)
            v2_rows += len(expanded)
            v2_distinct += 1
        _tally_rules(rule_breakdown, team, "v2", team.get("v2Defs") or [], V2_EVALUATION_CADENCE)

        summary.append(
            {
                "team": team["name"],
                "phase": team["phase"],
                "quality": team["quality"],
                "v1Rows": v1_rows,
                "v2Rows": v2_rows,
                "v1Distinct": v1_distinct,
                "v2Distinct": v2_distinct,
            }
        )

    # Unattributed orphans - match no team's registry (design section 6).
    unattributed_v1 = 0
    for definition in unattributed["v1"]:
        for timestamp in _row_timestamps(definition, V1_EVALUATION_CADENCE):
            unattributed_v1 += 1
            rows.append(
                {
                    "index": ES_CONFIG.v1_index,
                    "doc": {
                        "id": str(uuid.uuid4()),
                        "@timestamp": _iso(timestamp),
                        "application": definition["application"],
                        "object": definition["obj"],
                        "message": definition["message"],
                        "severity": code_for_name(definition["severity"]),
                        "operator": definition["operator"],
                        "key_field": v1_key_field(
                            definition["application"],
                            definition["obj"],
                            definition.get("node_name"),
                        ),
                        "time_created": _iso(timestamp),
                        "node_name": definition.get("node_name"),
                        "network": None,
                        "alert_rule_url": definition.get("alert_rule_url") or None,
                        "provider": definition.get("provider") or "grafana",
                    },
                }
            )

    unattributed_v2 = 0
    for definition in unattributed["v2"]:
        timestamps = _row_timestamps(definition, V2_EVALUATION_CADENCE)
        for index, timestamp in enumerate(timestamps):
            status = (
                "resolved"
                if definition.get("resolves") and index == len(timestamps) - 1
                else "firing"
            )
            doc = {
                "application": definition["application"],
                "component": definition["obj"],
                "message": definition["message"],
                "severity": code_for_name(definition["severity"]),
                "status": status,
                "impact": definition.get("impact"),
                "runbook_url": definition.get("runbook_url"),
                "environment": "production",
                "site": None,
                "node_name": None,
                "network": None,
                "alert_rule_url": None,
                "provider": "grafana",
            }
            unattributed_v2 += 1
            rows.append(
                {
                    "index": ES_CONFIG.v2_index,
                    "doc": {
                        "id": str(uuid.uuid4()),
                        "@timestamp": _iso(timestamp),
                        **doc,
                        "operator": definition["operator"],
                        "key_field": v2_key_field({**doc, "operator": definition["operator"]}),
                        "time_created": _iso(timestamp),
                    },
                }
            )

    stats_only = bool(os.environ.get("STATS_ONLY"))
    if not stats_only:
        if os.environ.get("RESET"):
            reset_indices()
        bulk_load(rows)

    _print_summary(summary)
    print(f"Unattributed: v1={unattributed_v1} v2={unattributed_v2}")

    # Written next to mock_teams.json, the definitions it summarizes, rather than to the
    # repository root. Deliberately not under test/fixtures/: that directory holds the
    # hand-authored acceptance oracle, and a generated file beside it invites exactly the
    # confusion the "never generate the oracle" rule exists to prevent.
    stats_path = Path(os.environ.get("STATS_PATH", SCRIPT_DIR / "mock-data-stats.json"))
    stats_path.write_text(
        json.dumps(
            {
                "generatedAt": _iso(NOW),
                "totalRows": len(rows),
                "summary": summary,
                "ruleBreakdown": rule_breakdown,
                "panelQueries": [
                    {
                        "team": t["name"],
                        "v1": t.get("v1PanelQuery"),
                        "v2": t.get("v2PanelQuery"),
                        "v1Operators": t.get("v1Operators"),
                        "v2Operator": t.get("v2Operator"),
                    }
                    for t in teams
                ],
                "unattributed": {
                    "v1Rows": unattributed_v1,
                    "v2Rows": unattributed_v2,
                    "v1Operators": [d["operator"] for d in unattributed["v1"]],
                    "v2Operators": [d["operator"] for d in unattributed["v2"]],
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"Stats written to {stats_path}")


def _tally_rules(
    breakdown: list[dict[str, Any]],
    team: dict[str, Any],
    schema: str,
    definitions: list[dict[str, Any]],
    cadence: timedelta,
) -> None:
    per_rule: dict[int, dict[str, int]] = {}
    for definition in definitions:
        row_count = (
            len(definition["rowsAt"])
            if definition.get("rowsAt")
            else _evaluation_rows(definition, cadence)[0]
        )
        for rule in definition.get("badRule") or []:
            entry = per_rule.setdefault(rule, {"rows": 0, "distinct": 0})
            entry["rows"] += row_count
            entry["distinct"] += 1
    for rule, entry in sorted(per_rule.items()):
        breakdown.append({"team": team["name"], "schema": schema, "rule": rule, **entry})


def _print_summary(summary: list[dict[str, Any]]) -> None:
    headers = ["team", "phase", "quality", "v1Rows", "v2Rows", "v1Distinct", "v2Distinct"]
    widths = {h: max(len(h), *(len(str(row[h])) for row in summary)) for h in headers}
    print(" | ".join(h.ljust(widths[h]) for h in headers))
    print("-+-".join("-" * widths[h] for h in headers))
    for row in summary:
        print(" | ".join(str(row[h]).ljust(widths[h]) for h in headers))


if __name__ == "__main__":
    main()
