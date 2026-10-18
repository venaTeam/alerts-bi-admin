"""Registry contract copies and pinned snapshot bytes must not silently drift."""

import hashlib
import json
from pathlib import Path

from alerts_bi_operations.registry import SCHEMA_PATH

ROOT = Path(__file__).resolve().parents[2]


def test_packaged_registry_schema_matches_the_pinned_contract() -> None:
    assert (
        SCHEMA_PATH.read_bytes()
        == (ROOT / "docs/upstream/contracts/teams.schema.json").read_bytes()
    )


def test_pinned_design_hashes_match() -> None:
    manifest = json.loads((ROOT / "docs/upstream/manifest.json").read_text("utf-8"))
    for relative, expected in manifest["sha256"].items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected, relative
