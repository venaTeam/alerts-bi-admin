"""Creates the "Alerts BI - scale probe" Kibana dashboard.

Shows how many DISTINCT alerts exist (application + key_field), how that splits by team, and
how it moves day to day.

  KIBANA_URL=https://kibana.internal:5601 KIBANA_AUTH=user:pass \
      uv run python scripts/create_kibana_panels.py

Kibana has no built-in unique-count across two fields, so this first adds an ``alert_uid``
RUNTIME FIELD to each data view that emits ``application|key_field`` - the primary key of an
alert (design doc 1.1). ``key_field`` alone is NOT sufficient: its application+object+node_name
form is only a default and a sender may override it.

Runtime fields are computed at query time and need inline painless enabled. At production
volume that is slow over long windows; if it strains the cluster, mint the same concatenation
as a real mapped keyword in an ingest pipeline and point the panels at that field instead.

Safe to re-run - every object is written with overwrite=true under a fixed id.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

KB = os.environ.get("KIBANA_URL", "http://localhost:5601").rstrip("/")
AUTH = os.environ.get("KIBANA_AUTH")

HEADERS = {"kbn-xsrf": "true", "Content-Type": "application/json"}
if AUTH:
    HEADERS["Authorization"] = "Basic " + base64.b64encode(AUTH.encode()).decode()


def _call(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{KB}{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        method=method,
        headers=HEADERS,
    )
    try:
        with urllib.request.urlopen(request) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # Kibana returns a structured error body; keep it rather than raising, so an
        # "already exists" runtime field stays a re-runnable no-op.
        payload = exc.read().decode("utf-8")
    result: dict[str, Any] = json.loads(payload) if payload else {}
    return result


def data_views() -> dict[str, str]:
    """Resolve data views by index title rather than hardcoding generated ids."""
    response = _call("GET", "/api/data_views")
    views = response.get("data_view") or []

    def find(title: str) -> str:
        for view in views:
            if view["title"] == title:
                view_id: str = view["id"]
                return view_id
        raise SystemExit(f'no data view for index "{title}" — create it in Kibana first')

    return {"v1": find("appchi-v1"), "v2": find("appchi-v2")}


def put(object_type: str, object_id: str, body: dict[str, Any]) -> dict[str, Any]:
    response = _call("POST", f"/api/saved_objects/{object_type}/{object_id}?overwrite=true", body)
    if response.get("error"):
        print(f"ERROR {object_id}: {response.get('message')}")
    else:
        print(f"ok  {object_type}/{response['id']}  — {response['attributes']['title']}")
    return response


def uniq(label: str) -> dict[str, Any]:
    return {
        "label": label,
        "dataType": "number",
        "operationType": "unique_count",
        "sourceField": "alert_uid",
        "isBucketed": False,
        "scale": "ratio",
        "params": {"emptyAsNull": False},
    }


def count(label: str) -> dict[str, Any]:
    return {
        "label": label,
        "dataType": "number",
        "operationType": "count",
        "sourceField": "___records___",
        "isBucketed": False,
        "scale": "ratio",
        "params": {"emptyAsNull": False},
    }


def ref(data_view_id: str) -> list[dict[str, str]]:
    return [
        {
            "type": "index-pattern",
            "id": data_view_id,
            "name": "indexpattern-datasource-layer-layer1",
        }
    ]


def _layers(columns: dict[str, Any], column_order: list[str]) -> dict[str, Any]:
    return {
        "formBased": {
            "layers": {
                "layer1": {
                    "columns": columns,
                    "columnOrder": column_order,
                    "incompleteColumns": {},
                }
            }
        }
    }


def main() -> None:
    views = data_views()

    # the runtime field the panels aggregate on
    for schema, view_id in views.items():
        response = _call(
            "POST",
            f"/api/data_views/data_view/{view_id}/runtime_field",
            {
                "name": "alert_uid",
                "runtimeField": {
                    "type": "keyword",
                    "script": {
                        "source": 'emit(doc["application"].value + "|" + doc["key_field"].value)'
                    },
                },
            },
        )
        message = response.get("message") or ""
        if response.get("error") and not re.search("already exists", message, re.IGNORECASE):
            print(f"ERROR {schema} runtime field: {message}")
        else:
            print(f"ok  runtime field alert_uid on {schema}")

    # ---- 1/2. big-number metric per schema ----
    for schema, view_id in views.items():
        put(
            "lens",
            f"alerts-bi-distinct-{schema}",
            {
                "attributes": {
                    "title": f"Distinct alerts — {schema} (application + key_field)",
                    "description": (
                        "Unique count of application|key_field, the primary key of an alert "
                        "(design doc 1.1)."
                    ),
                    "visualizationType": "lnsMetric",
                    "state": {
                        "datasourceStates": _layers(
                            {"col1": uniq("Distinct alerts"), "col2": count("Rows")},
                            ["col1", "col2"],
                        ),
                        "filters": [],
                        "query": {"language": "kuery", "query": ""},
                        "visualization": {
                            "layerId": "layer1",
                            "layerType": "data",
                            "metricAccessor": "col1",
                            "secondaryMetricAccessor": "col2",
                        },
                    },
                },
                "references": ref(view_id),
            },
        )

    # ---- 3. distinct alerts per day (the churn signal) ----
    put(
        "lens",
        "alerts-bi-distinct-per-day-v1",
        {
            "attributes": {
                "title": "Distinct alerts per day — v1",
                "description": (
                    "Compare the daily figure against the total over the window: if the total "
                    "is close to a single day, keys recur and the verdict cache hits. If it is "
                    "close to the sum of days, keys churn."
                ),
                "visualizationType": "lnsXY",
                "state": {
                    "datasourceStates": _layers(
                        {
                            "colX": {
                                "label": "@timestamp",
                                "dataType": "date",
                                "operationType": "date_histogram",
                                "sourceField": "@timestamp",
                                "isBucketed": True,
                                "scale": "interval",
                                "params": {
                                    "interval": "1d",
                                    "includeEmptyRows": True,
                                    "dropPartials": False,
                                },
                            },
                            "col1": uniq("Distinct alerts"),
                        },
                        ["colX", "col1"],
                    ),
                    "filters": [],
                    "query": {"language": "kuery", "query": ""},
                    "visualization": {
                        "legend": {"isVisible": True, "position": "right"},
                        "valueLabels": "hide",
                        "preferredSeriesType": "bar_stacked",
                        "layers": [
                            {
                                "layerId": "layer1",
                                "layerType": "data",
                                "seriesType": "bar_stacked",
                                "xAccessor": "colX",
                                "accessors": ["col1"],
                            }
                        ],
                    },
                },
            },
            "references": ref(views["v1"]),
        },
    )

    # ---- 4. distinct alerts by operator, with row count alongside ----
    put(
        "lens",
        "alerts-bi-distinct-by-operator-v1",
        {
            "attributes": {
                "title": "Distinct alerts by operator — v1",
                "description": (
                    "Rows next to distinct alerts: a large gap means one thing stuck, a small "
                    "gap means many different things firing (design doc 3.3)."
                ),
                "visualizationType": "lnsDatatable",
                "state": {
                    "datasourceStates": _layers(
                        {
                            "colB": {
                                "label": "Operator",
                                "dataType": "string",
                                "operationType": "terms",
                                "sourceField": "operator",
                                "isBucketed": True,
                                "scale": "ordinal",
                                "params": {
                                    "size": 50,
                                    "orderBy": {"type": "column", "columnId": "col1"},
                                    "orderDirection": "desc",
                                },
                            },
                            "col1": uniq("Distinct alerts"),
                            "col2": count("Rows"),
                        },
                        ["colB", "col1", "col2"],
                    ),
                    "filters": [],
                    "query": {"language": "kuery", "query": ""},
                    "visualization": {
                        "layerId": "layer1",
                        "layerType": "data",
                        "columns": [
                            {"columnId": "colB"},
                            {"columnId": "col1"},
                            {"columnId": "col2"},
                        ],
                    },
                },
            },
            "references": ref(views["v1"]),
        },
    )

    # ---- dashboard tying them together ----
    panels = [
        {"id": "alerts-bi-distinct-v1", "w": 12, "h": 8, "x": 0, "y": 0},
        {"id": "alerts-bi-distinct-v2", "w": 12, "h": 8, "x": 12, "y": 0},
        {"id": "alerts-bi-distinct-per-day-v1", "w": 24, "h": 12, "x": 0, "y": 8},
        {"id": "alerts-bi-distinct-by-operator-v1", "w": 24, "h": 14, "x": 0, "y": 20},
    ]
    put(
        "dashboard",
        "alerts-bi-scale",
        {
            "attributes": {
                "title": "Alerts BI — scale probe",
                "description": (
                    "How many DISTINCT alerts exist (application + key_field), how that splits "
                    "by team, and how it moves day to day."
                ),
                "timeRestore": True,
                "timeFrom": "now-90d",
                "timeTo": "now",
                "optionsJSON": json.dumps(
                    {"hidePanelTitles": False, "useMargins": True, "syncColors": False}
                ),
                "kibanaSavedObjectMeta": {
                    "searchSourceJSON": json.dumps(
                        {"query": {"language": "kuery", "query": ""}, "filter": []}
                    )
                },
                "panelsJSON": json.dumps(
                    [
                        {
                            "version": "8.15.0",
                            "type": "lens",
                            "gridData": {
                                "x": panel["x"],
                                "y": panel["y"],
                                "w": panel["w"],
                                "h": panel["h"],
                                "i": str(index + 1),
                            },
                            "panelIndex": str(index + 1),
                            "embeddableConfig": {},
                            "panelRefName": f"panel_{index}",
                        }
                        for index, panel in enumerate(panels)
                    ]
                ),
            },
            "references": [
                {"name": f"panel_{index}", "type": "lens", "id": panel["id"]}
                for index, panel in enumerate(panels)
            ],
        },
    )


if __name__ == "__main__":
    main()
