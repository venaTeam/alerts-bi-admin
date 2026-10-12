"""The repository instructions live in one file.

``AGENTS.md`` is canonical and ``CLAUDE.md`` points at it. They were byte-identical
192-line copies before, which is a standing invitation for one to drift from the other -
and a stale instruction file is worse than none, because it is followed.

These tests fail if the pointer grows back into a second copy.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AGENTS = ROOT / "AGENTS.md"
CLAUDE = ROOT / "CLAUDE.md"

#: Generous: the pointer is a dozen lines. Anything approaching the canonical file's length
#: is a copy, not a pointer.
MAX_POINTER_LINES = 30


def read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def test_both_instruction_files_exist() -> None:
    """Some tools look for one name, some for the other."""
    assert AGENTS.is_file()
    assert CLAUDE.is_file()


def test_agents_md_carries_the_actual_instructions() -> None:
    assert "SQL Server" in read(AGENTS)
    assert "migration" in read(AGENTS).lower()


def test_claude_md_is_a_pointer_not_a_copy() -> None:
    lines = read(CLAUDE).splitlines()
    assert len(lines) <= MAX_POINTER_LINES, (
        f"CLAUDE.md is {len(lines)} lines; it should point at AGENTS.md, not restate it"
    )


def test_claude_md_names_agents_md_so_a_reader_is_sent_there() -> None:
    assert "AGENTS.md" in read(CLAUDE)


def test_claude_md_imports_agents_md() -> None:
    """The ``@path`` line is what pulls the real instructions in automatically.

    The prose above it covers a reader that does not resolve the import.
    """
    assert any(line.strip() == "@AGENTS.md" for line in read(CLAUDE).splitlines())


def test_the_canonical_file_still_requires_reading_the_design() -> None:
    """The instructions' own first rule; losing it would be a silent, expensive regression."""
    text = read(AGENTS)
    assert "docs/upstream/alerts_bi_design.md" in text
    assert "canonical design" in text
