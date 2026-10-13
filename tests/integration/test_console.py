"""Unified operator workflow on real SQL Server and the existing Elasticsearch mock."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from alerts_bi_operations.review.publication import publish_run, unpublish_run
from alerts_bi_shared.db.connection import Database, connect
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError
from src.app import build_admin
from src.config import AdminConfig, AdminSettings
from src.engine.config import load_config
from src.engine.db.repositories import PersistencePayload, persist_run
from src.engine.es.client import EsClient
from src.pages import finding_key
from src.queries import decision_history

from tests.helpers.sql import sample_daily, sample_finding, sample_run

pytestmark = pytest.mark.integration
CONFIG = load_config()
DB = CONFIG.sql.test_database
TEAM = "checkout-api"
RUN = "1" * 64
OTHER = "2" * 64
END = datetime(2026, 8, 24)
USER = {"X-Forwarded-User": "alice"}


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    try:
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from alerts_bi_admin.engine.config import load_config; from alerts_bi_admin.engine.db.migrate import reset_test_database; config=load_config(); reset_test_database(config.sql, config.sql.test_database)",
            ],
            cwd=tmp_path_factory.mktemp("migrate"),
            check=True,
            capture_output=True,
        )
    except Exception as exc:
        pytest.skip(f"SQL Server unavailable: {exc}")
    with connect(CONFIG.sql, DB) as db:
        persist_run(
            db,
            PersistencePayload(
                run=sample_run(
                    run_id=RUN, run_at=END, window_end=END, window_start=END - timedelta(hours=168)
                ),
                daily_metrics=[sample_daily(run_id=RUN)],
                findings=[
                    sample_finding(
                        run_id=RUN,
                        message="Something <script>unsafe</script>",
                        quality_state="rule_flagged",
                        core_rule_ids="R1",
                        findings_evidence='[{"rule_id":"R1","matched_rows":4}]',
                    )
                ],
            ),
        )
        persist_run(
            db,
            PersistencePayload(
                run=sample_run(
                    run_id=OTHER, team_id="payments-api", team_display_name="Payments API"
                )
            ),
        )
    app = build_admin(
        AdminSettings(
            AdminConfig(CONFIG.sql),
            DB,
            "s" * 40,
            out_root=Path(tmp_path_factory.mktemp("console-out")),
        )
    )
    with TestClient(app, headers=USER, follow_redirects=False) as test_client:
        yield test_client


def token(client: TestClient) -> str:
    return str(client.get("/csrf").json()["token"])


def test_team_directory_and_selected_run_are_scoped(client: TestClient) -> None:
    home = client.get("/")
    assert home.status_code == 200
    assert "Checkout API" in home.text and "Latest run" in home.text
    page = client.get(f"/teams/{TEAM}?run_id={RUN}")
    assert page.status_code == 200
    assert page.text.index("Team runs") < page.text.index('id="selected-run"')
    assert RUN in page.text and OTHER not in page.text
    assert "Day by day" in page.text and "Panel SQL" in page.text and "Work list" in page.text
    assert 'value="fake"' in page.text and 'value="off"' in page.text
    assert client.get(f"/teams/{TEAM}?run_id={OTHER}").status_code == 404
    assert client.get(f"/teams/{TEAM}?run_id=missing").status_code == 404
    assert client.get(f"/teams/{TEAM}?tab=unknown").status_code == 422
    schedule = client.get("/schedule")
    assert schedule.status_code == 200 and "Weekly schedule" in schedule.text
    assert "data-team-selector" in schedule.text
    assert client.get("/decisions").status_code == 200
    assert (
        client.get(f"/teams/{TEAM}/summary?run_id={RUN}", follow_redirects=True).status_code == 200
    )


def test_findings_escape_evidence_and_gate_decisions_before_publication(client: TestClient) -> None:
    page = client.get(f"/teams/{TEAM}?run_id={RUN}&tab=findings")
    assert page.status_code == 200
    assert "matched_rows" in page.text
    assert "&lt;script&gt;" in page.text and "<script>unsafe" not in page.text
    assert "Publish this run before recording" in page.text
    assert 'action="/runs/' + RUN + '/decide"' not in page.text
    refused = client.post(
        f"/runs/{RUN}/decide",
        data={
            "csrf": token(client),
            "schema": "v1",
            "application": TEAM,
            "key_field": "checkout-api:cart:node-1",
            "finding": "R1",
            "state": "confirmed",
            "note": "Before publication",
        },
    )
    assert refused.status_code == 303 and "error=" in refused.headers["location"]


def test_publication_decision_and_withdrawal_keep_selected_run(client: TestClient) -> None:
    csrf = token(client)
    publish = client.post(f"/runs/{RUN}/publish", data={"csrf": csrf, "note": "Reviewed"})
    assert publish.status_code == 303 and "done=" in publish.headers["location"]
    assert f"run_id={RUN}" in publish.headers["location"]
    page = client.get(f"/teams/{TEAM}?run_id={RUN}&tab=findings")
    assert 'action="/runs/' + RUN + '/decide"' in page.text
    decision = client.post(
        f"/runs/{RUN}/decide",
        data={
            "csrf": csrf,
            "schema": "v1",
            "application": TEAM,
            "key_field": "checkout-api:cart:node-1",
            "finding": "R1",
            "state": "confirmed",
            "note": "Agreed after checking evidence",
        },
    )
    assert decision.status_code == 303 and "done=" in decision.headers["location"]
    history = client.get(decision.headers["location"])
    assert "Agreed after checking evidence" in history.text and "alice" in history.text
    assert "tab=findings" in decision.headers["location"]
    all_decisions = client.get("/decisions")
    assert all_decisions.status_code == 200
    assert "Agreed after checking evidence" in all_decisions.text
    assert "Checkout API" in all_decisions.text and "alice" in all_decisions.text
    key = finding_key(
        {"alert_schema": "v1", "application": TEAM, "key_field": "checkout-api:cart:node-1"}, "R1"
    )
    linked = client.get(f"/runs/{RUN}/findings?finding={key}", follow_redirects=True)
    assert linked.status_code == 200 and "finding=" + key in str(linked.url)
    assert "Agreed after checking evidence" in linked.text
    withdrawn = client.post(
        f"/runs/{RUN}/withdraw", data={"csrf": csrf, "reason": "Review needs correction"}
    )
    assert withdrawn.status_code == 303 and f"run_id={RUN}" in withdrawn.headers["location"]
    activity = client.get(f"/teams/{TEAM}?run_id={RUN}&tab=activity")
    assert "Review needs correction" in activity.text and "Withdrawn by alice" in activity.text
    with connect(CONFIG.sql, DB) as db:
        assert db.query_one(
            "SELECT COUNT(*) AS n FROM finding_decisions WHERE run_id=:id", {"id": RUN}
        ) == {"n": 1}
        assert db.query_one(
            "SELECT quality_state FROM alert_findings WHERE run_id=:id", {"id": RUN}
        ) == {"quality_state": "rule_flagged"}


def test_existing_output_routes_remain_available(client: TestClient) -> None:
    for name in ("scorecard.html", "daily_metrics.csv", "rule_counts.csv", "alert_worklist.csv"):
        response = client.get(f"/runs/{RUN}/{name}")
        assert response.status_code == 200
        if name.endswith(".csv"):
            assert "attachment" in response.headers["content-disposition"]


def test_identity_history_never_crosses_teams(client: TestClient) -> None:
    # The decision above belongs to checkout-api. Reusing its application/key in a
    # different team must not make that team's evidence view display the decision.
    with connect(CONFIG.sql, DB) as db:
        identity = [("v1", TEAM, "checkout-api:cart:node-1")]
        assert decision_history(db, identity, TEAM)
        assert not decision_history(db, identity, "payments-api")


def test_empty_team_and_failed_run_remain_navigable(client: TestClient) -> None:
    empty = client.get("/teams/payments-core")
    assert empty.status_code == 200 and "No runs yet" in empty.text
    assert "Team schedule log" in empty.text
    failed_id = "f" * 64
    with connect(CONFIG.sql, DB) as db:
        persist_run(
            db,
            PersistencePayload(
                run=sample_run(
                    run_id=failed_id,
                    run_at=END - timedelta(days=30),
                    status="failed",
                    completed_at=None,
                    error_summary="Source unavailable",
                )
            ),
        )
    failed = client.get(f"/teams/{TEAM}?run_id={failed_id}")
    assert failed.status_code == 200 and "Source unavailable" in failed.text
    assert "Only completed runs can be published" in failed.text
    assert f'action="/runs/{failed_id}/publish"' not in failed.text


def test_form_and_json_trigger_share_gate(client: TestClient) -> None:
    gate = client.app.state.run_gate  # type: ignore[attr-defined]
    assert gate.acquire()
    try:
        csrf = token(client)
        data = {"csrf": csrf, "run_at": "2026-08-24", "llm": "fake"}
        form = client.post(f"/teams/{TEAM}/trigger", data=data)
        assert form.status_code == 303 and "error=" in form.headers["location"]
        api = client.post(
            "/runs", headers={"X-CSRF-Token": csrf}, json={"team": TEAM, "llm": "fake"}
        )
        assert api.status_code == 409
    finally:
        gate.release()


def test_ui_trigger_runs_real_engine_and_stays_unpublished(client: TestClient) -> None:
    try:
        if not EsClient(CONFIG.es).index_exists(CONFIG.es.v1_index):
            pytest.skip("Elasticsearch mock index is absent")
    except Exception as exc:
        pytest.skip(f"Elasticsearch unavailable: {exc}")
    result = client.post(
        f"/teams/{TEAM}/trigger",
        data={"csrf": token(client), "run_at": "2026-08-31", "llm": "fake"},
    )
    assert result.status_code == 303 and "done=" in result.headers["location"], result.headers
    page = client.get(result.headers["location"])
    assert page.status_code == 200 and "Run completed" in page.text
    with connect(CONFIG.sql, DB) as db:
        run = db.query_one(
            "SELECT TOP 1 * FROM runs WHERE team_id=:team ORDER BY run_at DESC", {"team": TEAM}
        )
        assert run is not None and run["status"] == "completed"
        assert run["window_end"] - run["window_start"] == timedelta(hours=168)
        assert run["window_end"] == datetime(2026, 8, 31)
        assert run["window_start"] == datetime(2026, 8, 24)
        assert db.query_one(
            "SELECT COUNT(*) AS n FROM review_publications WHERE run_id=:id", {"id": run["run_id"]}
        ) == {"n": 0}


def test_decision_deep_link_finds_identity_beyond_first_page(client: TestClient) -> None:
    run_id = "3" * 64
    findings = [
        sample_finding(
            run_id=run_id,
            key_field=f"key-{i:03}",
            row_count=100 - i,
            quality_state="rule_flagged",
            core_rule_ids="R1,R4",
            findings_evidence='[{"rule_id":"R1","sample_evidence":{"normalized":"generic message"}}]',
        )
        for i in range(51)
    ]
    with connect(CONFIG.sql, DB) as db:
        persist_run(db, PersistencePayload(run=sample_run(run_id=run_id), findings=findings))
    key = finding_key(findings[-1], "R4")
    response = client.get(f"/runs/{run_id}/findings?finding={key}", follow_redirects=True)
    assert response.status_code == 200 and "page=2" in str(response.url)
    assert "key-050" in response.text and "Grafana alert without a rule link" in response.text
    assert "key-000" not in response.text


@pytest.mark.parametrize(
    "state, digit", [("unpublished", "4"), ("published", "5"), ("withdrawn", "6"), ("failed", "7")]
)
def test_permanent_deletion_removes_all_run_records(
    client: TestClient, state: str, digit: str
) -> None:
    run_id = digit * 64
    params = {"id": run_id}
    with connect(CONFIG.sql, DB) as db:
        persist_run(
            db,
            PersistencePayload(
                run=sample_run(
                    run_id=run_id,
                    team_id="payments-core",
                    status="failed" if state == "failed" else "completed",
                    window_start=END - timedelta(days=7),
                    window_end=END,
                ),
                daily_metrics=[sample_daily(run_id=run_id)],
                findings=[sample_finding(run_id=run_id)],
            ),
        )
        if state in ("published", "withdrawn"):
            publish_run(db, run_id, published_by="alice")
        if state == "withdrawn":
            unpublish_run(db, run_id, withdrawn_by="alice", reason="correction")
        with db.transaction():
            db.execute(
                "INSERT INTO finding_decisions (team_id,run_id,alert_schema,application,key_field,finding_id,state,note,decided_at,decided_by) VALUES ('payments-core',:id,'v1','checkout-api','key','R1','confirmed','test decision',SYSUTCDATETIME(),'alice')",
                params,
            )
            db.execute(
                "INSERT INTO weekly_review_log (invoked_at,team_id,window_end,outcome,run_id) VALUES (SYSUTCDATETIME(),'payments-core',:end,'stored',:id)",
                {**params, "end": END},
            )
            db.execute(
                "IF NOT EXISTS (SELECT 1 FROM llm_prompt_artifacts WHERE prompt_version='delete-test') INSERT INTO llm_prompt_artifacts VALUES ('delete-test',:id,'prompt',:id,'{}')",
                params,
            )
            db.execute(
                "INSERT INTO llm_review_scopes (scope_id,scope_kind,prompt_version,model_version,settings_hash,settings_json) VALUES (:id,'run','delete-test','test',:id,'{}')",
                params,
            )
            db.execute(
                "INSERT INTO llm_review_batches (cycle_id,scope_id,batch_id,cycle_number,request_hash,request_payload,status) VALUES (:id,:id,:id,1,:id,'{}','succeeded')",
                params,
            )
            db.execute(
                "INSERT INTO llm_review_attempts (cycle_id,attempt_number,status,created_at) VALUES (:id,1,'succeeded',SYSUTCDATETIME())",
                params,
            )
    # Without explicit confirmation the authenticated route must leave everything intact.
    assert client.post(f"/runs/{run_id}/delete", data={"csrf": token(client)}).status_code == 400
    gate = client.app.state.run_gate  # type: ignore[attr-defined]
    assert gate.acquire()
    try:
        assert (
            client.post(
                f"/runs/{run_id}/delete", data={"csrf": token(client), "confirm_delete": "1"}
            ).status_code
            == 409
        )
    finally:
        gate.release()
    out_root = client.app.state.settings.out_root  # type: ignore[attr-defined]
    directory = out_root / run_id[:16]
    directory.mkdir()
    (directory / "scorecard.html").write_text("test report")
    deleted = client.post(
        f"/runs/{run_id}/delete", data={"csrf": token(client), "confirm_delete": "1"}
    )
    assert deleted.status_code == 303 and "done=" in deleted.headers["location"]
    assert "run_id=" not in deleted.headers["location"]
    assert client.get(deleted.headers["location"]).status_code == 200
    assert not directory.exists()
    assert client.get(f"/runs/{run_id}/scorecard").status_code == 404
    assert client.get(f"/runs/{run_id}/alert_worklist.csv").status_code == 404
    with connect(CONFIG.sql, DB) as db:
        for table in (
            "runs",
            "daily_metrics",
            "daily_rule_counts",
            "alert_findings",
            "llm_batch_attempts",
            "run_panels",
            "review_publications",
            "finding_decisions",
            "weekly_review_log",
            "portal_reviews",
        ):
            assert db.query_one(f"SELECT COUNT(*) AS n FROM {table} WHERE run_id=:id", params) == {
                "n": 0
            }
        for table, column in (
            ("llm_review_scopes", "scope_id"),
            ("llm_review_batches", "scope_id"),
            ("llm_review_attempts", "cycle_id"),
        ):
            assert db.query_one(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {column}=:id", params
            ) == {"n": 0}
        assert db.query_one("SELECT 1 AS present FROM runs WHERE run_id=:id", {"id": OTHER})
        assert db.query_one(
            "SELECT 1 AS present FROM llm_prompt_artifacts WHERE prompt_version='delete-test'"
        )


def test_admin_ignores_forged_gap_override(client: TestClient) -> None:
    run_id = "8" * 64
    with connect(CONFIG.sql, DB) as db:
        # The 24 August and 14 September weeks leave two unpublished weeks in between.
        publish_run(db, RUN, published_by="alice")
        persist_run(
            db,
            PersistencePayload(
                run=sample_run(
                    run_id=run_id,
                    window_start=datetime(2026, 9, 7),
                    window_end=datetime(2026, 9, 14),
                )
            ),
        )
    try:
        page = client.get(f"/teams/{TEAM}?run_id={run_id}")
        assert 'name="allow_gap"' not in page.text
        response = client.post(
            f"/runs/{run_id}/publish", data={"csrf": token(client), "allow_gap": "1"}
        )
        assert response.status_code == 303 and "error=" in response.headers["location"]
        with connect(CONFIG.sql, DB) as db:
            assert (
                db.query_one(
                    "SELECT 1 AS present FROM review_publications WHERE run_id=:id", {"id": run_id}
                )
                is None
            )
    finally:
        with connect(CONFIG.sql, DB) as db:
            unpublish_run(db, RUN, withdrawn_by="alice", reason="test cleanup")


def test_decisions_remain_append_only_with_existing_parent(client: TestClient) -> None:
    with connect(CONFIG.sql, DB) as db:
        for statement in (
            "DELETE FROM finding_decisions WHERE run_id=:id",
            "UPDATE finding_decisions SET note='changed' WHERE run_id=:id",
        ):
            with pytest.raises(DBAPIError, match="append-only"), db.transaction():
                db.execute(statement, {"id": RUN})
        assert db.query_one(
            "SELECT COUNT(*) AS n FROM finding_decisions WHERE run_id=:id", {"id": RUN}
        ) == {"n": 1}


def test_failed_deletion_rolls_back_the_parent_and_publication(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "9" * 64
    with connect(CONFIG.sql, DB) as db:
        persist_run(db, PersistencePayload(run=sample_run(run_id=run_id, team_id="payments-core")))
        publish_run(db, run_id, published_by="alice")
    execute = Database.execute

    def fail_final_delete(db: Database, sql: str, params: dict[str, object] | None = None) -> None:
        if sql.startswith("DELETE FROM runs"):
            execute(db, sql, params)
            raise RuntimeError("injected transaction failure")
        execute(db, sql, params)

    with monkeypatch.context() as patched:
        patched.setattr(Database, "execute", fail_final_delete)
        with pytest.raises(RuntimeError, match="injected transaction failure"):
            client.post(
                f"/runs/{run_id}/delete", data={"csrf": token(client), "confirm_delete": "1"}
            )
    with connect(CONFIG.sql, DB) as db:
        assert db.query_one("SELECT 1 AS present FROM runs WHERE run_id=:id", {"id": run_id})
        assert db.query_one(
            "SELECT 1 AS present FROM portal_reviews WHERE run_id=:id", {"id": run_id}
        )
    assert (
        client.post(
            f"/runs/{run_id}/delete", data={"csrf": token(client), "confirm_delete": "1"}
        ).status_code
        == 303
    )


def test_decisions_cannot_reference_a_deleted_parent(client: TestClient) -> None:
    with (
        connect(CONFIG.sql, DB) as db,
        pytest.raises(DBAPIError, match="fk_finding_decisions_run"),
        db.transaction(),
    ):
        db.execute(
            "INSERT INTO finding_decisions (team_id,run_id,alert_schema,application,key_field,finding_id,state,note,decided_at,decided_by) VALUES ('checkout-api',:id,'v1','app','key','R1','confirmed','orphan',SYSUTCDATETIME(),'alice')",
            {"id": "0" * 64},
        )


@pytest.mark.parametrize("resource", ["alerts-bi-weekly", "llm-review:"])
def test_deletion_refuses_active_schedule_or_model_review(
    client: TestClient, resource: str
) -> None:
    run_id = "b" * 64
    with connect(CONFIG.sql, DB) as db:
        persist_run(db, PersistencePayload(run=sample_run(run_id=run_id)))
        name = resource + run_id if resource.endswith(":") else resource
        locked = db.query_one(
            "DECLARE @result INT; EXEC @result = sys.sp_getapplock @Resource=:resource, @LockMode='Exclusive', @LockOwner='Session', @LockTimeout=0; SELECT @result AS result",
            {"resource": name},
        )
        db.commit()
        assert locked is not None and locked["result"] >= 0
        try:
            response = client.post(
                f"/runs/{run_id}/delete", data={"csrf": token(client), "confirm_delete": "1"}
            )
            assert (
                response.status_code == 409
                if resource == "alerts-bi-weekly"
                else response.status_code == 303 and "error=" in response.headers["location"]
            )
            assert db.query_one("SELECT 1 AS present FROM runs WHERE run_id=:id", {"id": run_id})
        finally:
            db.execute(
                "EXEC sys.sp_releaseapplock @Resource=:resource, @LockOwner='Session'",
                {"resource": name},
            )
            db.commit()
    assert (
        client.post(
            f"/runs/{run_id}/delete", data={"csrf": token(client), "confirm_delete": "1"}
        ).status_code
        == 303
    )
