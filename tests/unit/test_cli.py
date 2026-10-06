"""Preserve required scope, actor defaults and withdrawal evidence at the new CLI."""

import argparse

import pytest
from src.cli import _operator, build_parser


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
