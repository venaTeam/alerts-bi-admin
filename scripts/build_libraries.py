"""Build the immutable shared and operations release artifacts from this repository."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    for name in ("alerts-bi-shared", "alerts-bi-operations"):
        subprocess.run(["uv", "build", "--package", name], cwd=ROOT, check=True)
    artifacts = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "dist").glob("alerts_bi_*.whl"))
    }
    print(json.dumps(artifacts, indent=2))


if __name__ == "__main__":
    main()
