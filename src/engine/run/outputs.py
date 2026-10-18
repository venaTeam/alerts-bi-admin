"""Write CSV exports to an explicitly chosen directory, for the CLI export commands."""

from pathlib import Path

from alerts_bi_operations.report.render import build_run_outputs
from alerts_bi_shared.db.connection import Database

CSV_FILES = ("daily_metrics.csv", "rule_counts.csv", "alert_worklist.csv")


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
