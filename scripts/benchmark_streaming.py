"""Read-only CPU/memory probe using the existing mock generator, no ES/SQL/model calls.

Run each mode in a fresh process for comparable peak memory:
  poetry run python scripts/benchmark_streaming.py --events 300000 --mode materialized
  poetry run python scripts/benchmark_streaming.py --events 300000 --mode streaming
  poetry run python scripts/benchmark_streaming.py --events 2000000 --mode streaming

This repeats the existing team's definitions across an exact week; it is a scale probe,
not an acceptance fixture or a production-throughput prediction. Fixture construction is
timed separately from JSON decoding and analysis. No second fixture is seeded or saved.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from datetime import timedelta
from importlib import import_module
from pathlib import Path
from time import perf_counter
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alerts_bi_operations.registry import load_registry, select_team
from alerts_bi_shared.window import build_run_window
from src.engine.domain.metrics import compute_daily_volume
from src.engine.domain.normalize import AlertRecord, normalize_row
from src.engine.rules.engine import attach_row_findings, evaluate_rows
from src.engine.run.streaming import SchemaAccumulator
from src.engine.suppression.evaluate import build_r5_findings, evaluate_suppression

from scripts.generate_mock_alerts import NOW, expand_v1


def peak_memory_mib() -> float:
    # Mypy recognizes this guard and checks the ctypes APIs only on Windows.
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                    "PrivateUsage",
                )
            ]

        process = ctypes.windll.kernel32.GetCurrentProcess
        process.restype = wintypes.HANDLE
        read = ctypes.windll.psapi.GetProcessMemoryInfo
        read.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not read(process(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError()
        return float(counters.PeakWorkingSetSize) / 2**20
    else:
        # resource is Unix-only; load dynamically so Windows type checks remain portable.
        resource = import_module("resource")

        scale = 2**20 if platform.system() == "Darwin" else 1024
        return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / scale


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=300000)
    parser.add_argument("--mode", choices=("streaming", "materialized"), default="streaming")
    parser.add_argument("--team", default="legacy-batch-jobs")
    parser.add_argument(
        "--identities", type=int, default=None, help="Distinct keys, independent of event count"
    )
    args = parser.parse_args()
    if args.events < 1:
        parser.error("--events must be positive")
    if args.identities is not None and not 1 <= args.identities <= args.events:
        parser.error("--identities must be between one and --events")
    definitions = json.loads(Path("scripts/mock_teams.json").read_bytes())["teams"]
    fixture = next(t for t in definitions if t["name"] == args.team)
    templates = [expand_v1(fixture, definition)[0]["doc"] for definition in fixture["v1Defs"]]
    team = select_team(load_registry(), args.team)
    window = build_run_window(NOW)
    panels = team.panels_for("v1")
    analysis = SchemaAccumulator("v1", window, panels)
    rows: list[AlertRecord] = []
    decode_seconds = analysis_seconds = generation_seconds = 0.0
    baseline = peak_memory_mib()
    total_us = 168 * 3600 * 1000000
    for offset in range(0, args.events, 1000):
        before = perf_counter()
        documents: list[dict[str, Any]] = []
        for i in range(offset, min(offset + 1000, args.events)):
            timestamp = (
                window.window_start + timedelta(microseconds=i * total_us // args.events)
            ).isoformat()
            documents.append(
                {
                    **templates[i % len(templates)],
                    "@timestamp": timestamp,
                    "time_created": timestamp,
                    **(
                        {"key_field": f"scale-key-{i % args.identities}"} if args.identities else {}
                    ),
                }
            )
        page = json.dumps(documents)
        del documents
        generation_seconds += perf_counter() - before
        before = perf_counter()
        batch = json.loads(page)
        decode_seconds += perf_counter() - before
        before = perf_counter()
        if args.mode == "streaming":
            for document in batch:
                analysis.add(normalize_row("v1", document, hash_document=False))
        else:
            rows.extend(normalize_row("v1", document) for document in batch)
        analysis_seconds += perf_counter() - before
        if (offset + 1000) % 250000 == 0:
            print(
                json.dumps({"processed": offset + 1000, "peak_mib": round(peak_memory_mib(), 1)}),
                flush=True,
            )
    before = perf_counter()
    if args.mode == "streaming":
        analysis.finish()
        daily = analysis.daily_volume()
        identities = len(analysis.identities)
    else:
        evaluated = evaluate_rows(rows, NOW)
        suppression = evaluate_suppression(rows, panels)
        attach_row_findings(evaluated, build_r5_findings(suppression.suppressed_row_ids, panels))
        daily = compute_daily_volume(rows, window)
        identities = len(evaluated.identities)
    analysis_seconds += perf_counter() - before
    assert sum(d.alerts for d in daily) == args.events
    print(
        json.dumps(
            {
                "mode": args.mode,
                "team": args.team,
                "events": args.events,
                "identities": identities,
                "baseline_peak_mib": round(baseline, 1),
                "peak_mib": round(peak_memory_mib(), 1),
                "decode_seconds": round(decode_seconds, 3),
                "analysis_seconds": round(analysis_seconds, 3),
                "fixture_generation_seconds": round(generation_seconds, 3),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
