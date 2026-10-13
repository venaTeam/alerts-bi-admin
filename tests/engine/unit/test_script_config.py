"""Tooling follows deployment index settings without issuing real writes."""

from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def script_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ES_V1_INDEX", "mock-custom-v1")
    monkeypatch.setenv("ES_V2_INDEX", "mock-custom-v2")
    monkeypatch.setenv("ES_URL", "http://localhost:9200")


def test_mock_rows_mappings_and_reset_use_configured_names(
    script_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = runpy.run_path(str(ROOT / "scripts/generate_mock_alerts.py"))
    definitions = json.loads((ROOT / "scripts/mock_teams.json").read_text(encoding="utf-8"))
    assert set(module["INDEX_MAPPINGS"]) == {"mock-custom-v1", "mock-custom-v2"}
    for schema in ("v1", "v2"):
        team = next(team for team in definitions["teams"] if team.get(schema + "Defs"))
        rows = module["expand_" + schema](team, team[schema + "Defs"][0])
        assert rows and {row["index"] for row in rows} == {"mock-custom-" + schema}
    requests: list[tuple[str, str]] = []

    def request(method: str, path: str, *args: Any) -> None:
        requests.append((method, path))

    monkeypatch.setitem(module["reset_indices"].__globals__, "_request", request)
    module["reset_indices"]()
    assert requests == [
        ("DELETE", "/mock-custom-v1"),
        ("PUT", "/mock-custom-v1"),
        ("DELETE", "/mock-custom-v2"),
        ("PUT", "/mock-custom-v2"),
    ]


@pytest.mark.parametrize("value", ["alerts-*", "_all", "a,b", "a/b", "mock-custom-v2"])
def test_mock_rejects_unsafe_or_colliding_write_targets(
    script_environment: None, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("ES_V1_INDEX", value)
    with pytest.raises(ValueError, match="mock ES_V1_INDEX"):
        runpy.run_path(str(ROOT / "scripts/generate_mock_alerts.py"))


def test_kibana_resolves_configured_index_titles(
    script_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = runpy.run_path(str(ROOT / "scripts/create_kibana_panels.py"))
    monkeypatch.setitem(
        module["data_views"].__globals__,
        "_call",
        lambda *args: {
            "data_view": [
                {"title": "mock-custom-v1", "id": "view1"},
                {"title": "mock-custom-v2", "id": "view2"},
            ]
        },
    )
    assert module["data_views"]() == {"v1": "view1", "v2": "view2"}


def test_scale_probe_uses_custom_registry_and_index_names(
    script_environment: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {"teams": [{"team_id": "custom", "v1_operators": ["op1"], "v2_operator": "op2"}]}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ADMIN_REGISTRY_PATH", str(registry))
    monkeypatch.setattr("sys.argv", ["es_scale_probe.py", "--team", "custom"])
    module = runpy.run_path(str(ROOT / "scripts/es_scale_probe.py"))
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setitem(
        module["main"].__globals__, "probe", lambda *args, **kwargs: calls.append(args)
    )
    module["main"]()
    assert calls == [
        ("mock-custom-v1", "object", ["op1"]),
        ("mock-custom-v2", "component", ["op2"]),
    ]
