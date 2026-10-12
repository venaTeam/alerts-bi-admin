"""Report rendering (flow step 9).

Reads ONLY from SQL Server. Nothing is recomputed from Elasticsearch, and nothing is
rendered from in-memory pipeline results: a report that could be produced without the store
would make the store optional, and the store is the one part of this design that cannot be
skipped.

A rendering failure after persistence is recoverable - the analysis is committed, so
rendering can simply be retried against the same run id.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from alerts_bi_shared.db.connection import Database
from alerts_bi_shared.logging_setup import log

from alerts_bi_operations.db.reports import (
    get_batch_attempts,
    get_daily_metrics,
    get_findings,
    get_rule_counts,
    get_run,
    get_run_panels,
)
from alerts_bi_operations.report.csv_export import (
    alert_worklist_csv,
    daily_metrics_csv,
    rule_counts_csv,
)
from alerts_bi_operations.report.html import render_scorecard

__all__ = ["OUTPUT_FILES", "build_run_outputs", "render_run_report"]

#: The exact file contract. Nothing else is written.
OUTPUT_FILES: Final = (
    "scorecard.html",
    "daily_metrics.csv",
    "rule_counts.csv",
    "alert_worklist.csv",
)


def build_run_outputs(db: Database, run_id: str) -> dict[str, str]:
    """Render one stored run into the four approved documents, in memory.

    Separated from writing so a caller that serves a report over HTTP renders from the same
    committed rows as a caller that writes files, rather than growing a second rendering
    path that could drift from this one.
    """
    run = get_run(db, run_id)
    if run is None:
        raise ValueError(f"run {run_id} is not in the store; nothing to render")

    daily = get_daily_metrics(db, run_id)
    rule_counts = get_rule_counts(db, run_id)
    findings = get_findings(db, run_id)
    panels = get_run_panels(db, run_id)
    attempts = get_batch_attempts(db, run_id)

    return {
        "scorecard.html": render_scorecard(run, daily, rule_counts, findings, panels, attempts),
        "daily_metrics.csv": daily_metrics_csv(daily),
        "rule_counts.csv": rule_counts_csv(rule_counts),
        "alert_worklist.csv": alert_worklist_csv(findings),
    }


def render_run_report(db: Database, run_id: str, out_dir: Path | str) -> list[Path]:
    """Render one stored run into the four approved files."""
    outputs = build_run_outputs(db, run_id)

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for name in OUTPUT_FILES:
        path = directory / name
        path.write_text(outputs[name], encoding="utf-8", newline="")
        written.append(path)

    log.info("report.rendered", run_id=run_id, out_dir=str(directory), files=len(written))
    return written
