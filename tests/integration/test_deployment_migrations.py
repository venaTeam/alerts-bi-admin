"""The shipped init command upgrades the UI database once and propagates failures."""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alerts_bi_shared.db.connection import connect
from fastapi.testclient import TestClient
from src.app import build_admin
from src.config import AdminConfig, AdminSettings
from src.engine.config import load_config
from src.engine.db import migrate as migrations
from src.engine.weekly.runner import schedule_lock

pytestmark = pytest.mark.integration
CONFIG = load_config()
DB = CONFIG.sql.test_database
ROOT = Path(__file__).resolve().parents[2]


def init_command(tmp_path: Path, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    manifest = (ROOT / "deploy/admin.yaml").read_text()
    match = re.search(r"- '(exec alerts-bi-admin db setup[^\n]+)'", manifest)
    assert match is not None, "The shipped init command must be exercised"
    shell = "/bin/sh" if os.name != "nt" else "C:/Program Files/Git/usr/bin/sh.exe"
    if not Path(shell).is_file():
        shell = shutil.which("sh") or ""
    assert shell, "A POSIX shell is required to verify the Linux deployment command"
    env = {**os.environ, **extra_env}
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [shell, "-ec", match.group(1)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_init_applies_missing_revision_once_to_admin_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def to_previous_head(sql: object, database: str) -> tuple[list[str], list[str]]:
        command.upgrade(migrations.alembic_config(CONFIG.sql, database), "010_run_jobs")
        return [], []

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "migrate", to_previous_head)
        migrations.reset_test_database(CONFIG.sql, DB)
    with connect(CONFIG.sql, DB) as db:
        db.execute(
            "INSERT INTO run_jobs (job_id,idempotency_key,requested_by,request_hash,team_id,"
            "run_at,llm_mode,registry_sha256,run_id,status,submitted_at) "
            "VALUES (:id,'existing','deployment-test',:hash,'checkout-api',SYSUTCDATETIME(),"
            "'off',:hash,:hash,'failed',SYSUTCDATETIME())",
            {"id": "c" * 32, "hash": "0" * 64},
        )
        db.commit()
    settings = AdminSettings(AdminConfig(CONFIG.sql), DB, "m" * 40)
    with TestClient(
        build_admin(settings), headers={"X-Forwarded-User": "migration-test"}
    ) as client:
        before = client.get("/healthz")
    assert before.status_code == 503, before.text
    assert before.json()["checks"]["operator_schema"]["ok"] is False
    # An explicit admin target wins even when SQL_DATABASE names another database.
    first = init_command(tmp_path, {"ADMIN_DATABASE": DB, "SQL_DATABASE": "unused-target"})
    assert first.returncode == 0, first.stderr
    assert "applied 011_run_job_keys" in first.stdout
    # Empty admin override falls back to SQL_DATABASE on a retry/new pod.
    second = init_command(tmp_path, {"ADMIN_DATABASE": "", "SQL_DATABASE": DB})
    assert second.returncode == 0, second.stderr
    assert "database is up to date" in second.stdout
    with TestClient(
        build_admin(settings), headers={"X-Forwarded-User": "migration-test"}
    ) as client:
        after = client.get("/healthz")
    assert after.status_code == 200, after.text
    assert after.json()["ok"] is True
    with connect(CONFIG.sql, DB) as db:
        assert migrations.current_revision(db) == "016_case_sensitive_key_fields"
        assert db.query_one(
            "SELECT COUNT(*) AS n FROM schema_migrations WHERE version='011_run_job_keys'"
        ) == {"n": 1}
        row = db.query_one("SELECT * FROM run_jobs WHERE job_id=:id", {"id": "c" * 32})
        assert row is not None and row["idempotency_key"] == "existing"
        assert row["idempotency_key_hash"] is not None


def test_init_command_fails_when_sql_is_unavailable(tmp_path: Path) -> None:
    # Reserve a local port without a listener; do not alter the existing SQL mock.
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        result = init_command(
            tmp_path,
            {
                "ADMIN_DATABASE": DB,
                "SQL_HOST": "127.0.0.1",
                "SQL_PORT": str(unavailable.getsockname()[1]),
                "SQL_REQUEST_TIMEOUT_MS": "1000",
            },
        )
    assert result.returncode != 0
    assert "database is up to date" not in result.stdout


def test_execution_lock_supports_schema_before_durable_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def before_jobs(sql: object, database: str) -> tuple[list[str], list[str]]:
        command.upgrade(migrations.alembic_config(CONFIG.sql, database), "009_run_deletion")
        return [], []

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "migrate", before_jobs)
        migrations.reset_test_database(CONFIG.sql, DB)
    try:
        with schedule_lock(CONFIG, DB), connect(CONFIG.sql, DB) as db:
            assert db.query_one("SELECT OBJECT_ID('run_jobs', 'U') AS table_id") == {
                "table_id": None
            }
            assert migrations.current_revision(db) == "009_run_deletion"
        # A released lock can be acquired again; the new slot query isn't required
        # by historical schemas with no durable manual-job feature.
        with schedule_lock(CONFIG, DB):
            pass
    finally:
        migrations.migrate(CONFIG.sql, DB)
