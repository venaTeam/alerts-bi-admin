"""The HTTP trigger surface, against the real mock Elasticsearch and a disposable database.

Integration rather than unit tests on purpose: the surface's whole job is to reach the same
pipeline the CLI reaches, so a test that stubbed the pipeline out would assert only that the
router works.

Most tests drive the app through ``TestClient``, which exercises the full ASGI stack
including the exception handlers. One test boots the real uvicorn server, because
``TestClient`` speaks ASGI directly and so cannot catch a server that is wired up wrong.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from alerts_bi_operations.report.render import OUTPUT_FILES
from alerts_bi_shared.db.connection import connect
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.engine.api import build_app
from src.engine.api.service import RunGate
from src.engine.config import ApiSettings, load_config
from src.engine.db.migrate import reset_test_database
from src.engine.es.client import EsClient

pytestmark = pytest.mark.integration

CONFIG = load_config()

#: A Monday boundary inside the existing fixed-clock mock's historical data.
RUN_AT = "2026-08-24T00:00:00Z"
TEAM = "checkout-api"
JSON = {"Accept": "application/json"}


def run_body(**overrides: object) -> dict[str, object]:
    return {"team": TEAM, "run_at": RUN_AT, "llm": "fake", **overrides}


@pytest.fixture(scope="module")
def settings(tmp_path_factory: pytest.TempPathFactory) -> ApiSettings:
    try:
        if not EsClient(CONFIG.es).index_exists(CONFIG.es.v1_index):
            pytest.skip("mock Elasticsearch is missing the configured v1 index")
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"mock Elasticsearch is not reachable: {exc}")
    try:
        reset_test_database(CONFIG.sql, CONFIG.sql.test_database)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"SQL Server is not reachable: {exc}")

    return ApiSettings(
        CONFIG,
        database=CONFIG.sql.test_database,
        out_root=Path(tmp_path_factory.mktemp("api-out")),
    )


@pytest.fixture(scope="module")
def app(settings: ApiSettings) -> FastAPI:
    return build_app(settings)


@pytest.fixture(scope="module")
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


# ------------------------------------------------------------------ discovery


def test_the_index_lists_every_registered_team_and_offers_a_run_form(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'action="/runs"' in response.text
    assert TEAM in response.text


def test_the_team_list_comes_from_the_registry(client: TestClient) -> None:
    teams = {team["team_id"]: team for team in client.get("/teams").json()["teams"]}
    assert TEAM in teams
    assert teams[TEAM]["display_name"] == "Checkout API"
    assert teams[TEAM]["v1_operators"] == ["checkout", "Checkout-API"]


def test_health_reports_each_dependency_separately(client: TestClient) -> None:
    payload = client.get("/healthz").json()
    assert payload["checks"]["elasticsearch"]["ok"] is True
    assert payload["checks"]["sql_server"]["ok"] is True
    assert payload["ok"] is True


def test_an_unknown_route_is_a_404_rather_than_a_traceback(client: TestClient) -> None:
    response = client.get("/nope")
    assert response.status_code == 404
    assert "Traceback" not in response.text


# ------------------------------------------------------------------- openapi


def test_the_openapi_document_describes_the_surface(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert "/runs/{run_id}" not in schema["paths"]
    assert set(schema["paths"]) >= {
        "/healthz",
        "/teams",
        "/runs",
        "/runs/{run_id}/{name}",
    }
    assert schema["info"]["title"] == "Alerts BI"


def test_the_run_contract_is_published_rather_than_described_in_prose(
    client: TestClient,
) -> None:
    run_request = client.get("/openapi.json").json()["components"]["schemas"]["RunRequest"]
    assert run_request["required"] == ["team"], "team never defaults"
    assert run_request["properties"]["llm"]["default"] == "live"


def test_the_interactive_documentation_is_served(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200


# ----------------------------------------------------------------------- runs


def test_a_run_returns_only_json_even_when_html_is_requested(client: TestClient) -> None:
    response = client.post("/runs", json=run_body(), headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "scorecard" not in response.json()
    assert set(response.json()["files"]) == set(OUTPUT_FILES)
    assert len(response.headers["x-alerts-bi-run-id"]) == 64


def test_a_run_writes_only_csvs_and_html_export_is_unavailable(
    client: TestClient,
) -> None:
    response = client.post("/runs", json=run_body())
    out_dir = Path(response.headers["x-alerts-bi-out-dir"])
    assert sorted(p.name for p in out_dir.iterdir()) == sorted(
        name for name in OUTPUT_FILES if name.endswith(".csv")
    )
    run_id = response.headers["x-alerts-bi-run-id"]
    assert client.get(f"/runs/{run_id}/scorecard.html").status_code == 422


def test_a_json_caller_gets_the_summary_and_links_instead_of_html(client: TestClient) -> None:
    payload = client.post("/runs", json=run_body(), headers=JSON).json()
    assert payload["team_id"] == TEAM
    assert payload["phase"] == "phase_1"
    with connect(CONFIG.sql, CONFIG.sql.test_database) as db:
        rows = db.query_one(
            "SELECT SUM(alerts) AS n FROM daily_metrics WHERE run_id=:id AND alert_schema='v1'",
            {"id": payload["run_id"]},
        )
        distinct = db.query_one(
            "SELECT COUNT(*) AS n FROM alert_findings WHERE run_id=:id AND alert_schema='v1'",
            {"id": payload["run_id"]},
        )
        assert rows is not None and distinct is not None
        assert payload["v1"] == {"rows": rows["n"], "distinct": distinct["n"]}
    assert payload["llm_assessed"] is True
    assert "scorecard" not in payload
    assert set(payload["files"]) == set(OUTPUT_FILES)


def test_run_at_may_be_omitted_and_defaults_to_latest_monday(client: TestClient) -> None:
    """Omitted boundaries cover the most recent completed Monday week."""
    payload = client.post("/runs", json={"team": TEAM, "llm": "off"}, headers=JSON).json()
    assert payload["team_id"] == TEAM
    assert payload["llm_assessed"] is False


def test_the_same_team_and_clock_reuse_one_run_id(client: TestClient) -> None:
    first = client.post("/runs", json=run_body())
    second = client.post("/runs", json=run_body())
    assert first.headers["x-alerts-bi-run-id"] == second.headers["x-alerts-bi-run-id"]


# ------------------------------------------------------------- run refusals


def test_a_run_without_a_team_is_refused_rather_than_fanning_out(client: TestClient) -> None:
    response = client.post("/runs", json={"llm": "fake"}, headers=JSON)
    assert response.status_code == 422
    assert "team" in response.text


def test_an_empty_team_is_refused_too(client: TestClient) -> None:
    assert client.post("/runs", json=run_body(team=""), headers=JSON).status_code == 422


def test_an_unknown_team_is_rejected_before_any_elasticsearch_query(
    client: TestClient,
) -> None:
    response = client.post("/runs", json=run_body(team="not-a-team"), headers=JSON)
    assert response.status_code == 400
    assert "registry" in response.json()["detail"]


def test_a_malformed_run_at_is_rejected(client: TestClient) -> None:
    assert (
        client.post("/runs", json=run_body(run_at="last-tuesday"), headers=JSON).status_code == 422
    )


def test_an_unknown_llm_mode_is_rejected_rather_than_silently_defaulting(
    client: TestClient,
) -> None:
    response = client.post("/runs", json=run_body(llm="maybe"), headers=JSON)
    assert response.status_code == 422
    assert "llm" in response.text


def test_a_browser_gets_a_readable_page_when_validation_fails(client: TestClient) -> None:
    """The run form posts from the index page, so this must not come back as raw JSON."""
    response = client.post("/runs", json=run_body(llm="maybe"), headers={"Accept": "text/html"})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/html")
    assert "llm" in response.text


def test_a_second_concurrent_run_is_refused_rather_than_racing(
    client: TestClient, app: FastAPI
) -> None:
    """Two runs of the same team and clock would derive one run_id and fight over its rows."""
    gate: RunGate = app.state.run_gate
    assert gate.acquire(), "the gate should be free before this test takes it"
    try:
        response = client.post("/runs", json=run_body(), headers=JSON)
    finally:
        gate.release()
    assert response.status_code == 409
    assert "already in progress" in response.json()["detail"]


# -------------------------------------------------------------------- reports


def test_standalone_html_run_route_is_removed(client: TestClient) -> None:
    run_id = client.post("/runs", json=run_body()).headers["x-alerts-bi-run-id"]
    assert client.get(f"/runs/{run_id}").status_code == 404


@pytest.mark.parametrize("name", ["daily_metrics.csv", "rule_counts.csv", "alert_worklist.csv"])
def test_each_csv_export_is_served_from_the_stored_rows(client: TestClient, name: str) -> None:
    run_id = client.post("/runs", json=run_body()).headers["x-alerts-bi-run-id"]
    response = client.get(f"/runs/{run_id}/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert name in response.headers["content-disposition"]
    # RFC 4180 line endings survive the transport.
    assert response.content.count(b"\r\n") > 1


def test_latest_resolves_that_team_s_most_recent_run(client: TestClient) -> None:
    """Latest means the newest run_at, not the most recently executed run.

    The newer window is run FIRST here on purpose: if insertion or completion order were
    what decided it, this would return the older one.
    """
    team = "payments-core"
    newer = client.post("/runs", json=run_body(team=team, run_at=RUN_AT))
    client.post("/runs", json=run_body(team=team, run_at="2026-08-17T00:00:00Z"))

    response = client.get(f"/runs/latest/daily_metrics.csv?team={team}")
    assert response.status_code == 200
    assert response.headers["x-alerts-bi-run-id"] == newer.headers["x-alerts-bi-run-id"]


def test_latest_without_a_team_is_refused(client: TestClient) -> None:
    response = client.get("/runs/latest/daily_metrics.csv", headers=JSON)
    assert response.status_code == 400
    assert "team" in response.json()["detail"]


def test_an_unknown_run_id_is_a_404_not_an_empty_report(client: TestClient) -> None:
    response = client.get(f"/runs/{'f' * 64}/daily_metrics.csv", headers=JSON)
    assert response.status_code == 404
    assert "not in the store" in response.json()["detail"]


def test_only_the_three_approved_csv_outputs_are_reachable(client: TestClient) -> None:
    """The file contract is the file contract; the surface serves nothing else."""
    run_id = client.post("/runs", json=run_body()).headers["x-alerts-bi-run-id"]
    for name in ("findings.json", "runs.csv", "secrets.txt"):
        assert client.get(f"/runs/{run_id}/{name}", headers=JSON).status_code == 422, name


# ------------------------------------------------------------- the real server


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def test_the_uvicorn_server_actually_serves(settings: ApiSettings) -> None:
    port = _free_port()
    config = uvicorn.Config(build_app(settings), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = 15.0
        while not server.started and deadline > 0 and thread.is_alive():
            thread.join(0.1)
            deadline -= 0.1
        assert server.started, "uvicorn did not start"
        response = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=30)
        assert response.status_code == 200
        assert response.json()["ok"] is True
    finally:
        server.should_exit = True
        thread.join(timeout=15)
