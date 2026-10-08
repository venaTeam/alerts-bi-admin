"""Build the immutable in-process runtime from an explicit runs Git revision.

Usage: python scripts/vendor_runtime.py /path/to/alerts-bi-runs
Only tracked files at SOURCE_REVISION are exported. Local edits, credentials and outputs
are excluded. UI source remains in admin; this wheel provides analysis and API adapters.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

SOURCE_REVISION = "89f6c88aff7a7c225f4e2a20485ebd8660db3ddb"
RUNTIME_VERSION = "0.1.0+admin.20261008"
LIBRARY_VERSION = "0.1.2"
ROOT = Path(__file__).resolve().parents[1]


def build(repository: Path) -> None:
    source = ROOT / "out" / ("runtime-source-" + RUNTIME_VERSION)
    wheel = ROOT / "vendor" / f"alerts_bi_runs_runtime-{RUNTIME_VERSION}-py3-none-any.whl"
    if wheel.exists():
        raise SystemExit("Refusing to overwrite an immutable runtime wheel; use a new version.")
    archive = subprocess.check_output(
        [
            "git",
            "-C",
            str(repository),
            "archive",
            "--format=zip",
            SOURCE_REVISION,
            "src",
            "compat",
            "config/teams.json",
        ]
    )
    source.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as exported:
        exported.extractall(source)
    registry = ROOT / "config/teams.json"
    registry_bytes = (source / "config/teams.json").read_bytes()
    if registry.exists() and registry.read_bytes() != registry_bytes:
        raise SystemExit(
            "Preserve the locally modified registry; compare with the exported source first."
        )
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_bytes(registry_bytes)
    original = tomllib.loads(
        subprocess.check_output(
            ["git", "-C", str(repository), "show", SOURCE_REVISION + ":pyproject.toml"], text=True
        )
    )
    dependencies = [
        f"{dependency.split('==')[0]}=={LIBRARY_VERSION}"
        if dependency.startswith(("alerts-bi-shared==", "alerts-bi-operations=="))
        else dependency
        for dependency in original["project"]["dependencies"]
    ]
    packaging = original["tool"]["setuptools"]
    metadata = (
        '[project]\nname = "alerts-bi-runs-runtime"\n'
        f'version = "{RUNTIME_VERSION}"\nrequires-python = ">=3.12"\n'
        'description = "Pinned Alerts BI analysis runtime for the unified admin app"\n'
        f"dependencies = {json.dumps(dependencies)}\n"
        '[build-system]\nrequires = ["setuptools>=80,<81"]\nbuild-backend = "setuptools.build_meta"\n'
        f"[tool.setuptools]\npackages = {json.dumps(packaging['packages'])}\n"
        'package-dir = {"alerts_bi_runs" = "src", "src" = "compat/src"}\n'
        "[tool.setuptools.package-data]\nalerts_bi_runs = "
        + json.dumps(packaging["package-data"]["alerts_bi_runs"])
        + "\n"
    )
    (source / "pyproject.toml").write_text(metadata, encoding="utf-8")
    built = ROOT / "out" / ("runtime-wheel-" + RUNTIME_VERSION)
    subprocess.run(
        ["uv", "build", "--wheel", "--offline", "--quiet", "--out-dir", str(built)],
        cwd=source,
        check=True,
    )
    shutil.copy2(built / wheel.name, wheel)
    manifest = {
        "repository": "https://github.com/venaTeam/alerts-bi-runs",
        "revision": SOURCE_REVISION,
        "distribution": "alerts-bi-runs-runtime",
        "version": RUNTIME_VERSION,
        "source_changes": "Packaging metadata only: runtime name/version, pinned shared/operations dependencies, no console entry points.",
        "wheel": wheel.name,
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "registry": {
            "path": "config/teams.json",
            "sha256": hashlib.sha256(registry_bytes).hexdigest(),
        },
    }
    (ROOT / "vendor/runtime-source.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    build(parser.parse_args().repository.resolve())
