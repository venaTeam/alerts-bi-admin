"""Acceptance verification against the hand-reviewed manifest.

Requires the mock stack and a clean fixture load::

    docker compose up -d
    RESET=1 poetry run python scripts/generate_mock_alerts.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from src.engine.config import load_config
from src.engine.es.client import EsClient
from src.engine.run.verify import MANIFEST_PATH, verify_acceptance

pytestmark = pytest.mark.acceptance

CONFIG = load_config()
OUT_DIR = Path("out") / "acceptance-test"

ACCEPTANCE_TEAMS = (
    "acceptance-core",
    "acceptance-batching",
    "acceptance-suppression",
    "acceptance-blast-radius",
    "acceptance-fire-patterns",
    "acceptance-unseen",
)


@pytest.fixture(scope="module")
def stack() -> None:
    """Skip rather than fail when the mock stack is not up."""
    try:
        available = EsClient(CONFIG.es).index_exists(CONFIG.es.v1_index)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"mock Elasticsearch is not reachable: {exc}")
    if not available:
        pytest.skip(
            "mock Elasticsearch is missing the configured v1 index; run: docker compose up -d && "
            "RESET=1 poetry run python scripts/generate_mock_alerts.py"
        )
    return None


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(MANIFEST_PATH.read_bytes().decode("utf-8"))
    return loaded


@pytest.fixture(scope="module")
def verification(stack: None) -> Any:
    try:
        return verify_acceptance(
            config=CONFIG, out_dir=str(OUT_DIR), database=CONFIG.sql.test_database
        )
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"acceptance verification could not run: {exc}")


def test_persisted_rows_and_csv_exports_match_the_hand_reviewed_manifest(
    verification: Any,
) -> None:
    if not verification.ok:
        detail = "\n".join(
            f"  {failure.where}\n"
            f"    expected {json.dumps(failure.expected, default=str)}\n"
            f"    actual   {json.dumps(failure.actual, default=str)}"
            for failure in verification.failures
        )
        pytest.fail(
            f"{len(verification.failures)} of {verification.checks} acceptance checks "
            f"failed:\n{detail}"
        )
    assert verification.checks > 300, (
        f"expected a substantial number of checks, got {verification.checks}"
    )


def test_the_manifest_is_hand_authored_and_records_its_derivations(
    manifest: dict[str, Any],
) -> None:
    assert isinstance(manifest["_comment"], list)
    assert "computed BY HAND" in " ".join(manifest["_comment"])
    for team_id, team in manifest["teams"].items():
        assert isinstance(team["_why"], list), (
            f"{team_id} must record why its numbers are what they are"
        )


def test_the_manifest_covers_every_acceptance_path_the_design_calls_out(
    manifest: dict[str, Any],
) -> None:
    teams = manifest["teams"].keys()
    for required in ACCEPTANCE_TEAMS:
        assert required in teams, f"missing acceptance team {required}"

    # The over-200 group must split into balanced partitions differing by at most one.
    sizes = manifest["teams"]["acceptance-batching"]["llm_batches"]["partition_sizes_by_group"]
    big = sizes["https://grafana.internal/d/acc-batch-big"]
    assert big == [134, 134, 133]
    assert sum(big) == 401
    assert max(big) - min(big) <= 1


def test_the_acceptance_csv_exports_were_rendered_for_every_team(verification: Any) -> None:
    assert verification is not None
    for team_id in ACCEPTANCE_TEAMS:
        for name in (
            "daily_metrics.csv",
            "rule_counts.csv",
            "alert_worklist.csv",
        ):
            assert (OUT_DIR / team_id / name).exists(), f"{team_id}/{name}"
        assert not (OUT_DIR / team_id / "scorecard.html").exists()
