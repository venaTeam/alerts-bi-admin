"""Preserve required scope, actor defaults and withdrawal evidence at the new CLI."""

import argparse
import json
from pathlib import Path

import pytest
from src.cli import _operator, build_parser, main


@pytest.mark.parametrize(
    "arguments",
    [["publish"], ["unpublish", "--run-id", "run"], ["decisions"], ["publications"]],
)
def test_operator_commands_require_their_scope_and_evidence(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(arguments)
    assert exc.value.code == 2


def test_explicit_actor_is_preserved() -> None:
    assert _operator(argparse.Namespace(by="alice")) == "alice"


def test_publication_options_are_preserved() -> None:
    args = build_parser().parse_args(
        ["publish", "--run-id", "run", "--by", "alice", "--replace", "--allow-gap"]
    )
    assert args.run_id == "run" and args.by == "alice"
    assert args.replace and args.allow_gap


def test_registry_check_accepts_shared_operators_and_non_slug_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = tmp_path / "teams.json"
    registry.write_text(
        json.dumps(
            {
                "registry_version": "test",
                "teams": [
                    {
                        "team_id": team_id,
                        "display_name": team_id,
                        "v1_operators": ["shared", "shared"],
                        "v2_operator": "shared",
                    }
                    for team_id in ["Team_A", "Team B"]
                ],
            }
        ),
        encoding="utf-8",
    )
    assert main(["registry", "check", "--registry", str(registry)]) == 0
    output = capsys.readouterr()
    assert "is valid: 2 teams" in output.out
    assert "Team_A" in output.out and "Team B" in output.out
    assert not output.err
