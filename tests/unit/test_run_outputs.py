"""Only the exact legacy run HTML is removed, preserving CSV bytes and other files."""

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


def test_cleanup_only_removes_scorecards_with_all_run_exports(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    for name in outputs.CSV_FILES:
        (run / name).write_bytes(b"keep\r\n")
    (run / "scorecard.html").write_text("generated")
    (tmp_path / "scorecard.html").write_text("unrelated")
    assert outputs.remove_run_html(tmp_path) == [run / "scorecard.html"]
    assert (tmp_path / "scorecard.html").read_text() == "unrelated"
    assert all((run / name).read_bytes() == b"keep\r\n" for name in outputs.CSV_FILES)
