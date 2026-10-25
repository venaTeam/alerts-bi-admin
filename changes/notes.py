"""Draft and check this repository's change notes (see changes/README.md).

  python changes/notes.py new [--base origin/main] [--force]
      Write changes/<today>-<branch>.md with the facts of this branch filled in from the diff
      (migrations, settings, Dockerfile, CI, packages) and TODO where only the author knows:
      the title, what changed and why, deploy order, other steps, what to port.

  python changes/notes.py check [--base origin/main]
      Fail unless this branch adds a note, the note has no TODO left, and its facts match the
      diff. CI runs this on every pull request.

The branch is compared with its merge base on --base, including uncommitted and untracked
files. Standard library only, so CI can run it without installing the project.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
NOTES = "changes"
ENV_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", re.MULTILINE)
MIGRATION = re.compile(r"^(\d{3,})_\w+$")
#: A template placeholder such as "<branch name>" left in a hand-written note.
PLACEHOLDER = re.compile(r"<[A-Za-z][^<>\n]*\s[^<>\n]*>")
FACT_LINE = re.compile(r"^- (Migrations|Settings|Dockerfile|CI|Packages):(.*)$", re.MULTILINE)


class Fail(Exception):
    """The command cannot continue; the message says why."""


def git(*args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise Fail(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def show(base: str, path: str) -> str:
    return git("show", f"{base}:{path}", check=False)


def read(path: str) -> str:
    file = ROOT / path
    return file.read_text("utf-8", errors="replace") if file.is_file() else ""


@dataclass
class Change:
    status: str  # A, M, D or R
    path: str
    old: str = ""


def changes(base: str) -> list[Change]:
    """Tracked changes from `base` to the working tree, plus untracked files."""
    found = []
    parts = git("diff", "--name-status", "-z", "--find-renames", base).split("\0")
    index = 0
    while index < len(parts) and parts[index]:
        status = parts[index][0]
        if status in "RC":
            found.append(Change("R" if status == "R" else "A", parts[index + 2], parts[index + 1]))
            index += 3
        else:
            found.append(Change(status, parts[index + 1]))
            index += 2
    for path in git("ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if path:
            found.append(Change("A", path))
    return found


def is_note(path: str) -> bool:
    pure = PurePosixPath(path)
    return pure.parent.as_posix() == NOTES and pure.suffix == ".md" and pure.name != "README.md"


def lock_versions(text: str) -> dict[str, str]:
    if not text:
        return {}
    try:
        packages = tomllib.loads(text).get("package", [])
    except tomllib.TOMLDecodeError:
        return {}
    return {str(item["name"]).lower(): str(item["version"]) for item in packages}


@dataclass
class Facts:
    migrations: list[str] = field(default_factory=list)
    settings_added: list[str] = field(default_factory=list)
    settings_removed: list[str] = field(default_factory=list)
    dockerfile: list[str] = field(default_factory=list)
    ci: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)  # "name 1.0 → 1.1", "name new 2.0", ...
    package_names: list[str] = field(default_factory=list)


def facts(base: str, found: list[Change]) -> Facts:
    result = Facts()
    for change in found:
        pure = PurePosixPath(change.path)
        name = pure.name
        if "migrations" in pure.parts and change.status == "A":
            match = MIGRATION.match(pure.stem)
            if match and pure.stem not in result.migrations:
                result.migrations.append(pure.stem)
        if name in ("Dockerfile", "Containerfile", ".dockerignore") or name.endswith(".Dockerfile"):
            result.dockerfile.append(change.path)
        if pure.parts[0] == ".github":
            result.ci.append(change.path)
        if name.endswith(".env.example"):
            before = set(ENV_KEY.findall(show(base, change.old or change.path)))
            after = set(ENV_KEY.findall(read(change.path)))
            result.settings_added += sorted(after - before)
            result.settings_removed += sorted(before - after)
        if name == "poetry.lock":
            old = lock_versions(show(base, change.path))
            new = lock_versions(read(change.path))
            for package in sorted(set(old) | set(new)):
                if package not in old:
                    result.packages.append(f"{package} new {new[package]}")
                elif package not in new:
                    result.packages.append(f"{package} removed")
                elif old[package] != new[package]:
                    result.packages.append(f"{package} {old[package]} → {new[package]}")
                else:
                    continue
                result.package_names.append(package)
    return result


def merge_base(base: str) -> str:
    if not git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False).strip():
        raise Fail(f"{base} does not exist. Run `git fetch origin` or pass --base.")
    return git("merge-base", base, "HEAD").strip()


def branch_name() -> str:
    name = git("symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
    if not name or name in ("main", "master"):
        raise Fail("Run this on the pull request's branch, not on main or a detached HEAD.")
    return name


def draft(branch: str, base: str, found: Facts) -> str:
    subjects = git("log", "--no-merges", "--format=- %s", f"{base}..HEAD").strip()
    subjects = subjects or "- (no commits yet)"
    settings = []
    if found.settings_added:
        settings.append("added " + ", ".join(found.settings_added))
    if found.settings_removed:
        settings.append("removed " + ", ".join(found.settings_removed))
    dockerfile = (
        f"changed ({', '.join(found.dockerfile)}). TODO: the logic to port, for example "
        '"copy the new docs/ folder"'
        if found.dockerfile
        else "no change"
    )
    ci = (
        f"changed ({', '.join(found.ci)}). TODO: what to port to GitLab CI, or "
        '"GitHub-only, do not port"'
        if found.ci
        else "no change"
    )
    return f"""# TODO: pull request title

Branch: {branch}

## What changed

TODO: what behavior or structure changed, and why, for a reader who has not seen the pull
request. Commits on this branch:
{subjects}

## Deploy

- Migrations: {", ".join(found.migrations) or "none"}
- Settings: {"; ".join(settings) or "none"}
- Order: TODO: deploy order across repositories, or "any"
- Other steps: TODO: manual steps, or "none"

## Private network

- Dockerfile: {dockerfile}
- CI: {ci}
- Packages: {"; ".join(found.packages) or "none"}
"""


def cmd_new(args: argparse.Namespace) -> None:
    branch = branch_name()
    base = merge_base(args.base)
    found = changes(base)
    existing = [change.path for change in found if change.status == "A" and is_note(change.path)]
    if existing and not args.force:
        raise Fail(
            f"This branch already has a note: {', '.join(existing)}. Edit it, or pass --force."
        )
    slug = re.sub(r"[^a-z0-9]+", "-", branch.rsplit("/", 1)[-1].lower()).strip("-")
    path = ROOT / NOTES / f"{date.today().isoformat()}-{slug}.md"
    for old in existing:
        if args.force and (ROOT / old) != path:
            (ROOT / old).unlink()
    path.write_bytes(draft(branch, base, facts(base, found)).encode("utf-8"))
    print(f"Wrote {path.relative_to(ROOT).as_posix()}. Replace every TODO, then commit it.")


def mentions(text: str, word: str) -> bool:
    return re.search(rf"(?<![\w.-]){re.escape(word)}(?![\w-])", text, re.IGNORECASE) is not None


def cmd_check(args: argparse.Namespace) -> None:
    base = merge_base(args.base)
    found = changes(base)
    problems = []
    for change in found:
        if is_note(change.path) and change.status != "A":
            problems.append(f"{change.path}: merged notes are never edited, moved or deleted.")
        if change.old and is_note(change.old):
            problems.append(f"{change.old}: merged notes are never edited, moved or deleted.")
    notes = [change.path for change in found if change.status == "A" and is_note(change.path)]
    if not notes:
        raise Fail(
            "This branch adds no change note. Run `python changes/notes.py new`, replace every "
            "TODO, and commit the note (changes/README.md)."
        )
    text = "\n".join(read(path) for path in notes)
    for path in notes:
        if "TODO" in read(path):
            problems.append(f"{path}: replace every TODO.")
        if PLACEHOLDER.search(read(path)):
            problems.append(f"{path}: replace every <...> placeholder from the template.")
    lines: dict[str, str] = {}
    for key, value in FACT_LINE.findall(text):
        lines[key] = (lines.get(key, "") + " " + value).strip()
    for key in ("Migrations", "Settings", "Dockerfile", "CI", "Packages"):
        if key not in lines:
            problems.append(f"The note has no '- {key}:' line (changes/README.md).")
    got = facts(base, found)
    nothing = re.compile(r"^(none|no change)\.?$", re.IGNORECASE)
    for key, needed in (
        ("Migrations", got.migrations),
        ("Settings", got.settings_added + got.settings_removed),
        ("Packages", got.package_names),
    ):
        for item in needed:
            if not mentions(lines.get(key, ""), item):
                problems.append(f"'- {key}:' does not mention {item}, which this branch changes.")
    for key, changed in (("Dockerfile", got.dockerfile), ("CI", got.ci)):
        if changed and nothing.match(lines.get(key, "")):
            problems.append(
                f"'- {key}:' says no change, but this branch changes {', '.join(changed)}."
            )
    if problems:
        raise Fail("\n".join(problems))
    print(f"Change note OK: {', '.join(notes)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new", help="draft the note for this branch")
    new.add_argument("--base", default="origin/main")
    new.add_argument("--force", action="store_true", help="replace this branch's note")
    new.set_defaults(run=cmd_new)
    check = commands.add_parser("check", help="fail unless this branch has a complete note")
    check.add_argument("--base", default="origin/main")
    check.set_defaults(run=cmd_check)
    args = parser.parse_args()
    try:
        args.run(args)
    except Fail as fail:
        print(fail, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
