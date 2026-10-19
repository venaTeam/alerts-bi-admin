"""The consolidated CLI and immutable resources work without the former service."""

from pathlib import Path

import pytest
from src.cli import build_parser
from src.engine.db.migrate import heads
from src.engine.llm.prompt import build_prompt


def test_default_prompt_and_revision_graph_work_outside_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert build_prompt().system_prompt_hash == (
        "f9754a11f4e9e0ba438c1f2d9062fa9511d57dc67789346ff01fc4e18489b794"
    )
    assert heads() == ["019_erase_operator_names"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["run", "--team", "checkout-api", "--fake-llm"],
        ["db", "status"],
        ["db", "setup"],
        ["weekly", "--dry-run"],
        ["weekly-status"],
        ["report", "--run-id", "example"],
        ["verify-acceptance"],
        ["serve"],
        ["run-worker", "--once"],
        ["registry", "check"],
        ["publish", "--run-id", "example"],
    ],
)
def test_admin_registers_operational_commands(arguments: list[str]) -> None:
    assert build_parser().parse_args(arguments).command == arguments[0]
