"""Configured dependencies and CLI overrides reach the actual service adapters."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from src.cli import _command_registry, build_parser
from src.config import load_config as load_admin_config
from src.engine import cli
from src.engine.api import service
from src.engine.config import ApiSettings, load_config


@pytest.mark.parametrize("missing", [None, "legacy-*", "current"])
def test_health_checks_both_configured_indexes_and_closes_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: str | None
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ES_V1_INDEX", "legacy-*")
    monkeypatch.setenv("ES_V2_INDEX", "current")
    es = MagicMock()
    es.index_exists.side_effect = lambda index: index != missing
    monkeypatch.setattr(service, "EsClient", lambda config: es)
    monkeypatch.setattr(service, "connect", MagicMock())
    checks = service.check_health(ApiSettings(load_config()))
    assert checks["elasticsearch"].ok is (missing is None)
    assert [call.args[0] for call in es.index_exists.call_args_list] == ["legacy-*", "current"]
    es.close.assert_called_once()


@pytest.mark.parametrize("explicit", [False, True])
def test_weekly_cli_honors_registry_and_output_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, explicit: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_REGISTRY_PATH", "mounted/teams.json")
    monkeypatch.setenv("ADMIN_OUT_DIR", "mounted/reports")
    from src.engine.weekly import runner

    weekly = MagicMock(return_value=[])
    monkeypatch.setattr(runner, "run_weekly", weekly)
    args = build_parser().parse_args(
        ["weekly", "--no-llm", "--dry-run"]
        + (["--registry", "explicit.json", "--out", "explicit-reports"] if explicit else [])
    )
    assert cli.dispatch(args) == 0
    kwargs = weekly.call_args.kwargs
    assert kwargs["registry_path"] == ("explicit.json" if explicit else "mounted/teams.json")
    assert kwargs["out_root"] == Path("explicit-reports" if explicit else "mounted/reports/weekly")


@pytest.mark.parametrize("command", ["run", "report"])
@pytest.mark.parametrize("explicit", [False, True])
def test_cli_report_directories_honor_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str, explicit: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_OUT_DIR", "mounted/reports")
    monkeypatch.setenv("ADMIN_REGISTRY_PATH", "mounted/teams.json")
    monkeypatch.setattr(cli, "connect", MagicMock())
    monkeypatch.setattr(cli, "EsClient", MagicMock())
    summary = MagicMock(run_id="a" * 64)
    summary.readiness = None
    execute = MagicMock(return_value=(MagicMock(), summary))
    monkeypatch.setattr(cli, "execute_run", execute)
    monkeypatch.setattr(cli, "persist_run", MagicMock())
    render = MagicMock(return_value={})
    monkeypatch.setattr(cli, "render_run_report", render)
    args = build_parser().parse_args(
        (
            ["run", "--team", "team", "--no-llm"]
            if command == "run"
            else ["report", "--run-id", "a" * 64]
        )
        + (
            [
                "--out",
                "explicit-out",
                *(["--registry", "explicit.json"] if command == "run" else []),
            ]
            if explicit
            else []
        )
    )
    assert cli.dispatch(args) == 0
    assert render.call_args.args[2] == Path(
        "explicit-out" if explicit else "mounted/reports/" + "a" * 16
    )
    if command == "run":
        assert execute.call_args.kwargs["registry_path"] == (
            "explicit.json" if explicit else "mounted/teams.json"
        )


def test_registry_check_uses_environment_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_REGISTRY_PATH", "mounted/teams.json")
    from alerts_bi_operations import registry

    loaded = MagicMock(teams=[])
    check = MagicMock(return_value=loaded)
    monkeypatch.setattr(registry, "load_registry", check)
    args = build_parser().parse_args(["registry", "check"])
    assert _command_registry(args, load_admin_config()) == 0
    check.assert_called_once_with("mounted/teams.json")
