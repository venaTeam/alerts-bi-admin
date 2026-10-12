"""Build the immutable shared and operations release artifacts from this repository."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    for directory in ("shared", "operations"):
        subprocess.run(
            ["poetry", "build", "--output", str(ROOT / "dist")],
            cwd=ROOT / "packages" / directory,
            check=True,
        )
    artifacts = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "dist").glob("alerts_bi_*.whl"))
    }
    print(json.dumps(artifacts, indent=2))


if __name__ == "__main__":
    main()
