"""Require real disposable services and zero skipped integration/acceptance checks."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from alerts_bi_shared.db.connection import connect
from src.engine.config import load_config
from src.engine.es.client import EsClient


class NoSkippedChecks:
    def __init__(self) -> None:
        self.skipped: set[str] = set()

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.skipped:
            self.skipped.add(str(report.nodeid))


def main() -> int:
    config = load_config()
    if (
        config.sql.host not in ("localhost", "127.0.0.1")
        or config.sql.test_database != "alerts_bi_test"
    ):
        raise RuntimeError("CI integration requires local SQL Server and alerts_bi_test")
    if config.es.url.rstrip("/") not in ("http://localhost:9200", "http://127.0.0.1:9200"):
        raise RuntimeError("CI integration requires the explicit local ES mock")
    with connect(config.sql, "master") as db:
        db.query_one("SELECT 1 AS ready")
    es = EsClient(config.es)
    try:
        for name in (config.es.v1_index, config.es.v2_index):
            if not es.index_exists(name):
                raise RuntimeError("Seed both ES mock indices before running integration")
    finally:
        es.close()
    guard = NoSkippedChecks()
    result = pytest.main(
        ["tests/integration", "tests/engine/integration", "tests/engine/acceptance", "-q", "-rs"],
        plugins=[guard],
    )
    if guard.skipped:
        print("CI integration refused skipped checks: " + ", ".join(sorted(guard.skipped)))
        return 1
    return int(result)


if __name__ == "__main__":
    raise SystemExit(main())
