"""changes/notes.py against a temporary git repository.

The same file is in admin, portal and design, next to the same notes.py; keep them identical.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

NOTES = Path(__file__).resolve().parents[2] / "changes" / "notes.py"
LOCK = '[[package]]\nname = "httpx"\nversion = "{}"\n'


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return result.stdout


def notes(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(repo / "changes" / "notes.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo,
    )


def write(repo: Path, files: dict[str, str]) -> None:
    for inner, text in files.items():
        path = repo / inner
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())


def note(
    migrations: str = "none",
    settings: str = "none",
    dockerfile: str = "no change",
    ci: str = "no change",
    packages: str = "none",
    changed: str = "Explains the change.",
) -> str:
    return f"""# Sample change

Branch: fix/sample

## What changed

{changed}

## Deploy

- Migrations: {migrations}
- Settings: {settings}
- Order: any
- Other steps: none

## Private network

- Dockerfile: {dockerfile}
- CI: {ci}
- Packages: {packages}
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main with one merged note, and the branch fix/sample checked out."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "core.autocrlf", "false")
    (root / "changes").mkdir()
    shutil.copy(NOTES, root / "changes" / "notes.py")
    write(
        root,
        {
            "Dockerfile": "FROM python\n",
            ".env.example": "A=1\n",
            "poetry.lock": LOCK.format("0.27.0"),
            ".github/workflows/checks.yml": "on: push\n",
            "changes/2026-01-01-merged.md": note(),
        },
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(root, "checkout", "-q", "-b", "fix/sample")
    return root


def change_everything(repo: Path) -> None:
    write(
        repo,
        {
            "src/db/migrations/021_sample.sql": "SELECT 1;\n",
            ".env.example": "A=1\nB=2\n",
            "Dockerfile": "FROM python\nCOPY docs/ docs/\n",
            ".github/workflows/checks.yml": "on: [push, pull_request]\n",
            "poetry.lock": LOCK.format("0.28.1"),
        },
    )


def test_a_branch_without_a_note_fails(repo: Path) -> None:
    write(repo, {"app.py": "x = 1\n"})
    result = notes(repo, "check")
    assert result.returncode == 1 and "adds no change note" in result.stderr


def test_new_drafts_the_facts_and_check_rejects_the_todos(repo: Path) -> None:
    change_everything(repo)
    assert notes(repo, "new").returncode == 0
    (drafted,) = (repo / "changes").glob("*-sample.md")
    text = drafted.read_text("utf-8")
    assert "- Migrations: 021_sample\n" in text
    assert "- Settings: added B\n" in text
    assert "- Dockerfile: changed (Dockerfile). TODO:" in text
    assert "- CI: changed (.github/workflows/checks.yml). TODO:" in text
    assert "- Packages: httpx 0.27.0 → 0.28.1\n" in text
    result = notes(repo, "check")
    assert result.returncode == 1 and "replace every TODO" in result.stderr


def test_a_completed_note_passes(repo: Path) -> None:
    change_everything(repo)
    complete = note(
        migrations="021_sample (back up first)",
        settings="added B",
        dockerfile="copy the new docs/ folder",
        ci="GitHub-only, do not port",
        packages="httpx 0.27.0 → 0.28.1",
        changed="Removes a TODO comment from the code.",
    )
    write(repo, {"changes/2026-02-01-sample.md": complete})
    result = notes(repo, "check")
    assert result.returncode == 0, result.stderr


def test_a_note_that_contradicts_the_diff_fails(repo: Path) -> None:
    change_everything(repo)
    write(repo, {"changes/2026-02-01-sample.md": note()})
    result = notes(repo, "check")
    assert result.returncode == 1
    for missing in ("021_sample", "mention B", "httpx", "'- Dockerfile:'", "'- CI:'"):
        assert missing in result.stderr, missing


def test_a_renamed_dockerfile_counts_as_a_dockerfile(repo: Path) -> None:
    write(repo, {"docker/Dockerfile.worker": "FROM python\n", "changes/2026-02-01-s.md": note()})
    result = notes(repo, "check")
    assert result.returncode == 1 and "docker/Dockerfile.worker" in result.stderr


def test_editing_a_merged_note_fails(repo: Path) -> None:
    write(repo, {"changes/2026-01-01-merged.md": note(changed="Rewritten.")})
    write(repo, {"changes/2026-02-01-sample.md": note()})
    result = notes(repo, "check")
    assert result.returncode == 1 and "never edited" in result.stderr


def test_a_template_placeholder_fails(repo: Path) -> None:
    write(repo, {"changes/2026-02-01-sample.md": note(changed="<What changed, and why.>")})
    result = notes(repo, "check")
    assert result.returncode == 1 and "placeholder" in result.stderr


def test_new_refuses_to_run_on_main(repo: Path) -> None:
    git(repo, "checkout", "-q", "main")
    result = notes(repo, "new")
    assert result.returncode == 1 and "not on main" in result.stderr
