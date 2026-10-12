"""Paging correctness, cleanup, and fail-closed behavior without a live cluster."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from alerts_bi_shared.window import build_run_window
from src.engine.domain.normalize import AlertRecord
from src.engine.es.client import ElasticsearchError, EsClient
from src.engine.es.reader import scan_schema

from tests.engine.helpers.rows import v1_row

WINDOW = build_run_window(datetime(2026, 8, 25, 18, tzinfo=UTC))


class Client:
    config = SimpleNamespace(page_size=1)

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = iter(responses)
        self.requests: list[dict[str, Any]] = []
        self.closed: list[str] = []

    def open_pit(self, index: str) -> str:
        assert index == "appchi-v1"
        return "initial"

    def search(self, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(deepcopy(body))
        return next(self.responses)

    def close_pit(self, pit: str) -> None:
        self.closed.append(pit)


def page(n: int, *, total: int = 2, empty: bool = False) -> dict[str, Any]:
    return {
        "pit_id": f"pit-{n}",
        "took": 1,
        "timed_out": False,
        "_shards": {"failed": 0},
        "hits": {
            "total": {"relation": "eq", "value": total},
            "hits": [] if empty else [{"_source": v1_row().source, "sort": [n, n]}],
        },
    }


def test_counts_once_uses_latest_pit_and_preserves_search_after() -> None:
    client = Client([page(1), page(2), page(3, empty=True)])
    received: list[AlertRecord] = []
    assert scan_schema(cast(EsClient, client), "v1", ["op"], WINDOW, received.append) == (3, 2)
    assert len(received) == 2
    assert [r["track_total_hits"] for r in client.requests] == [True, False, False]
    assert [r["pit"]["id"] for r in client.requests] == ["initial", "pit-1", "pit-2"]
    assert client.requests[1]["search_after"] == [1, 1]
    assert client.requests[2]["search_after"] == [2, 2]
    assert all(r["allow_partial_search_results"] is False for r in client.requests)
    assert client.closed == ["pit-3"]


@pytest.mark.parametrize("failure", ["timeout", "shard", "total", "relation", "sort", "count"])
def test_incomplete_reads_fail_and_close_latest_pit(failure: str) -> None:
    response = page(1)
    if failure == "timeout":
        response["timed_out"] = True
    elif failure == "shard":
        response["_shards"]["failed"] = 1
    elif failure == "total":
        del response["hits"]["total"]
    elif failure == "relation":
        response["hits"]["total"]["relation"] = "gte"
    elif failure == "sort":
        del response["hits"]["hits"][0]["sort"]
    client = Client([response, page(2, empty=True)])
    with pytest.raises(ElasticsearchError):
        scan_schema(cast(EsClient, client), "v1", ["op"], WINDOW, lambda row: None)
    assert client.closed == ["pit-2" if failure == "count" else "pit-1"]


def test_callback_failure_closes_pit() -> None:
    client = Client([page(1)])

    def fail(row: Any) -> None:
        raise ValueError("analysis failed")

    with pytest.raises(ValueError, match="analysis failed"):
        scan_schema(cast(EsClient, client), "v1", ["op"], WINDOW, fail)
    assert client.closed == ["pit-1"]


def test_no_operators_skips_cluster() -> None:
    client = Client([])
    assert scan_schema(cast(EsClient, client), "v1", [], WINDOW, lambda row: None) == (0, 0)
    assert client.requests == []
    assert client.closed == []
