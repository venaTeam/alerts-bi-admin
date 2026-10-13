"""Persist CSV exports; serve HTML on demand from SQL instead of storing run HTML."""

from pathlib import Path

from alerts_bi_operations.report.render import build_run_outputs
from alerts_bi_shared.db.connection import Database

CSV_FILES = ("daily_metrics.csv", "rule_counts.csv", "alert_worklist.csv")


def remove_run_html(out_root: Path) -> list[Path]:
    """Remove legacy scorecards only inside an explicit output root with matching CSVs."""
    root = out_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Select an existing run output directory.")
    removed = []
    for path in root.rglob("scorecard.html"):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            continue
        if path.is_file() and all((path.parent / name).is_file() for name in CSV_FILES):
            path.unlink()
            removed.append(path)
    return removed


def render_run_report(db: Database, run_id: str, out_dir: Path | str) -> list[Path]:
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    outputs = build_run_outputs(db, run_id)
    written = []
    for name, content in outputs.items():
        if name.endswith(".csv"):
            path = directory / name
            path.write_text(content, encoding="utf-8", newline="")
            written.append(path)
    # Remove the exact legacy artifact only inside the explicitly selected output directory.
    html = directory / "scorecard.html"
    if html.is_file() and not html.is_symlink():
        html.unlink()
    return written
