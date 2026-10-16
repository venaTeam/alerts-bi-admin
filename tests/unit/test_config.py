"""Independent operator configuration and application boundaries."""

import ast
from pathlib import Path

import pytest
from src.config import load_admin_settings, load_config


def test_admin_needs_no_pipeline_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SQL_DATABASE", raising=False)
    monkeypatch.delenv("ADMIN_DATABASE", raising=False)
    monkeypatch.setenv("ES_PAGE_SIZE", "not-an-integer")
    monkeypatch.setenv("LLM_TIMEOUT_MS", "not-an-integer")
    monkeypatch.setenv("ADMIN_SECRET", "unit-test-placeholder-" * 3)
    (tmp_path / ".env").write_text("SQL_DATABASE=operator_database\n", encoding="utf-8")
    config = load_config()
    settings = load_admin_settings(config)
    assert settings.config is config
    assert settings.database == config.sql.database == "operator_database"


def test_database_and_registry_overrides_are_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_SECRET", "unit-test-placeholder-" * 3)
    monkeypatch.setenv("ADMIN_DATABASE", "admin_database")
    monkeypatch.setenv("ADMIN_REGISTRY_PATH", "mounted/teams.json")
    settings = load_admin_settings()
    assert settings.database == "admin_database"
    assert settings.registry_path == "mounted/teams.json"
    explicit = load_admin_settings(database="explicit_database", registry_path="other/teams.json")
    assert explicit.database == "explicit_database"
    assert explicit.registry_path == "other/teams.json"


def test_operator_ui_uses_only_local_engine_adapters() -> None:
    sources = Path(__file__).resolve().parents[2] / "src"
    forbidden = ("alerts_bi_runs", "alerts_bi_portal", "elasticsearch", "openai")
    adapters = {
        "engine.config",
        "engine.api.negotiation",
        "engine.api.routers.runs",
        "engine.api.schemas",
        "engine.api.service",
        "engine.api.jobs",
        "engine.api.backfill",
        "engine.weekly.plan",
        "engine.run.outputs",
        "engine.cli",
    }
    modules = list(sources.glob("*.py"))
    assert modules
    for path in modules:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(forbidden), path
                if node.module.startswith("engine"):
                    assert node.module in adapters, path
            elif isinstance(node, ast.Import):
                assert all(not item.name.startswith(forbidden) for item in node.names), path


def test_listener_passes_environment_indexes_to_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_SECRET", "x" * 40)
    monkeypatch.setenv("ES_V1_INDEX", "operator-v1")
    monkeypatch.setenv("ES_V2_INDEX", "operator-v2")
    settings = load_admin_settings().api_settings()
    assert settings.config.es.v1_index == "operator-v1"
    assert settings.config.es.v2_index == "operator-v2"
