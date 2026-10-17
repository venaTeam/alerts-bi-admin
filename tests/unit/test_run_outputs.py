"""An explicit CSV export removes only the exact legacy run HTML, preserving other files."""

from pathlib import Path
from typing import cast

import pytest
from alerts_bi_shared.db.connection import Database
from src.engine.run import outputs


def test_csv_export_removes_legacy_html_and_preserves_other_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        outputs,
        "build_run_outputs",
        lambda db, run: {
            "daily_metrics.csv": "a\r\n1\r\n",
        },
    )
    (tmp_path / "scorecard.html").write_text("legacy")
    (tmp_path / "notes.html").write_text("keep")
    paths = outputs.render_run_report(cast(Database, None), "run", tmp_path)
    assert [p.name for p in paths] == ["daily_metrics.csv"]
    assert paths[0].read_bytes() == b"a\r\n1\r\n"
    assert not (tmp_path / "scorecard.html").exists()
    assert (tmp_path / "notes.html").read_text() == "keep"
