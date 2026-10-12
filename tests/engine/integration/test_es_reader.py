"""Elasticsearch reader against the local mock cluster.

Read-only: these never write to or delete from the indices.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from alerts_bi_operations.registry import load_registry, select_team
from alerts_bi_shared.window import build_run_window
from src.engine.config import load_config
from src.engine.es.client import EsClient
from src.engine.es.reader import V1_INDEX, build_query, read_schema, read_team_alerts

pytestmark = pytest.mark.integration

CONFIG = load_config()
#: The mock dataset is generated against this fixed clock.
MOCK_NOW = datetime.fromisoformat("2026-08-25T18:00:00+00:00")
WINDOW = build_run_window(MOCK_NOW)


@pytest.fixture(scope="module")
def client() -> EsClient:
    es = EsClient(CONFIG.es)
    if not es.index_exists(V1_INDEX):
        pytest.skip(
            "mock Elasticsearch is not reachable; run: docker compose up -d && "
            "RESET=1 uv run python scripts/generate_mock_alerts.py"
        )
    return es


def test_the_query_is_scoped_to_exact_operators_and_the_half_open_window() -> None:
    query = build_query(["a", "b"], WINDOW)
    filters = query["bool"]["filter"]
    assert filters[0] == {"terms": {"operator": ["a", "b"]}}
    # Milliseconds are always written, even at a whole second: one instant format is used
    # everywhere, and the hashed run and batch identifiers depend on it.
    assert filters[1]["range"]["@timestamp"]["gte"] == "2026-08-18T18:00:00.000Z"
    assert filters[1]["range"]["@timestamp"]["lt"] == "2026-08-25T18:00:00.000Z"
    assert "lte" not in filters[1]["range"]["@timestamp"]


def test_reads_only_the_selected_team_operators(client: EsClient) -> None:
    team = select_team(load_registry(), "fraud-detection")
    result = read_schema(client, "v1", team.v1_operators, WINDOW)
    assert result.rows, "expected the fraud-detection fixture to have rows in the window"
    assert {r.operator for r in result.rows} == {"fraud-detection"}


def test_operator_matching_is_exact_and_case_sensitive(client: EsClient) -> None:
    exact = read_schema(client, "v1", ["batch-team"], WINDOW)
    wrong_case = read_schema(client, "v1", ["BATCH-TEAM"], WINDOW)
    assert exact.rows
    assert wrong_case.rows == []


def test_a_team_listing_several_case_variants_gets_all_of_them(client: EsClient) -> None:
    team = select_team(load_registry(), "legacy-batch-jobs")
    result = read_schema(client, "v1", team.v1_operators, WINDOW)
    operators = {r.operator for r in result.rows}
    assert operators <= set(team.v1_operators)
    assert len(operators) > 1, "expected several operator variants for this team"


def test_every_returned_row_falls_inside_the_half_open_window(client: EsClient) -> None:
    team = select_team(load_registry(), "checkout-api")
    result = read_schema(client, "v1", team.v1_operators, WINDOW)
    assert result.rows
    for row in result.rows:
        assert WINDOW.window_start <= row.timestamp < WINDOW.window_end


def test_pagination_is_exact_a_tiny_page_size_returns_the_same_rows(client: EsClient) -> None:
    team = select_team(load_registry(), "legacy-batch-jobs")
    big = read_schema(client, "v1", team.v1_operators, WINDOW, page_size=5000)
    small = read_schema(client, "v1", team.v1_operators, WINDOW, page_size=37)

    assert len(small.rows) == len(big.rows)
    assert small.pages > big.pages, "the small page size should have needed more pages"
    # Same multiset of documents, order-independent: paging must not skip or duplicate.
    assert sorted(r.doc_hash for r in small.rows) == sorted(r.doc_hash for r in big.rows)


def test_the_retrieved_row_count_matches_what_the_cluster_reports(client: EsClient) -> None:
    team = select_team(load_registry(), "data-pipeline-etl")
    result = read_schema(client, "v1", team.v1_operators, WINDOW, page_size=500)
    assert len(result.rows) == result.reported_total


def test_a_team_with_no_v2_operator_skips_the_v2_query_entirely(client: EsClient) -> None:
    team = select_team(load_registry(), "legacy-batch-jobs")
    assert team.v2_operator is None
    result = read_team_alerts(client, team, WINDOW)["v2"]
    assert (result.rows, result.pages, result.reported_total) == ([], 0, 0)


def test_a_fully_migrated_team_returns_v2_rows_and_no_v1_rows(client: EsClient) -> None:
    team = select_team(load_registry(), "payments-core")
    read = read_team_alerts(client, team, WINDOW)
    assert read["v1"].rows == []
    assert read["v2"].rows
    assert {r.operator for r in read["v2"].rows} == {"payments-core"}


def test_unattributed_alerts_belong_to_no_team_and_are_never_returned(client: EsClient) -> None:
    registry = load_registry()
    claimed = {op for team in registry.teams for op in team.v1_operators}
    claimed |= {t.v2_operator for t in registry.teams if t.v2_operator}

    for team in registry.teams:
        read = read_team_alerts(client, team, WINDOW)
        for row in [*read["v1"].rows, *read["v2"].rows]:
            assert row.operator in claimed, f"row carried unclaimed operator {row.operator}"


def test_rows_keep_their_complete_source_document(client: EsClient) -> None:
    team = select_team(load_registry(), "payments-core")
    row = read_team_alerts(client, team, WINDOW)["v2"].rows[0]
    assert isinstance(row.source, dict)
    assert "key_field" in row.source
    assert "@timestamp" in row.source
    assert row.schema == "v2"
