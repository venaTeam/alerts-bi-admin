"""Opt-in, isolated evaluation of the actual alert-review protocol.

Default fake mode exercises plumbing only. Live mode also requires LLM_LIVE_TEST=true,
an explicit SQL database and reviewed labels (or explicit --allow-draft). It never calls
execute_run/persist_run, queries production verdicts or publishes a review.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alerts_bi_shared.db.connection import connect
from alerts_bi_shared.hashing import compact_json, sha256_of, sha256_text
from src.engine.config import load_config
from src.engine.db.llm_audit import SqlLlmJournal
from src.engine.llm.assess import assess_alerts
from src.engine.llm.client import LlmClient
from src.engine.llm.evaluation import compare_trials, score_trial, validate_corpus
from src.engine.llm.fake import FakeLlmClient
from src.engine.llm.openai_client import OpenAiLlmClient
from src.engine.llm.prompt import build_prompt
from src.engine.llm.response import response_json_schema

from scripts.generate_mock_alerts import review_documents

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fake", "live"), default="fake")
    parser.add_argument("--cases", type=Path, default=ROOT / "test/fixtures/llm-review-cases.json")
    parser.add_argument("--split", choices=("development", "holdout"), default="development")
    parser.add_argument("--caps", type=int, nargs="+", default=[1, 10, 25, 50, 100, 200])
    parser.add_argument(
        "--representations", choices=("factored", "full"), nargs="+", default=["factored", "full"]
    )
    parser.add_argument(
        "--orders", choices=("normal", "reverse"), nargs="+", default=["normal", "reverse"]
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument(
        "--continue-after-failure",
        action="store_true",
        help="Continue the matrix after a failed attempt; default stops before trying more settings",
    )
    parser.add_argument(
        "--database",
        help="Explicit SQL audit database for live trials; migrations must already be applied",
    )
    parser.add_argument(
        "--allow-draft",
        action="store_true",
        help="Allow provisional labels for exploratory live trials",
    )
    parser.add_argument(
        "--system-prompt",
        type=Path,
        help="Frozen baseline/candidate system prompt; requires --prompt-version",
    )
    parser.add_argument("--prompt-version", help="Distinct version for a supplied frozen prompt")
    parser.add_argument(
        "--compare", type=Path, help="Earlier summary for paired comparison of the first trials"
    )
    parser.add_argument(
        "--out", type=Path, help="Summary JSON path; no alert text or responses are written here"
    )
    args = parser.parse_args(argv)
    if any(cap < 1 or cap > 200 for cap in args.caps) or args.repeats < 1:
        parser.error("caps must be 1..200 and repeats must be positive")
    if bool(args.system_prompt) != bool(args.prompt_version):
        parser.error("--system-prompt and --prompt-version must be supplied together")
    if args.prompt_version and len(args.prompt_version) > 32:
        parser.error("prompt version must fit the 32-character SQL contract")
    manifest = json.loads(args.cases.read_bytes())
    documents = review_documents(manifest["cases"])
    corpus = validate_corpus(manifest, documents)
    cases = [case for case in corpus if case.split == args.split]
    if not cases:
        parser.error("selected split has no cases")
    config = load_config()
    if args.mode == "live":
        if not config.llm.live_test or not args.database:
            parser.error("live trials require LLM_LIVE_TEST=true and an explicit --database")
        if not config.llm.base_url or not config.llm.model:
            parser.error("live trials require a configured endpoint and model")
        if manifest["review_state"] != "reviewed" and not args.allow_draft:
            parser.error("draft annotations need review or explicit --allow-draft for exploration")
    prompt = build_prompt()
    system = (
        args.system_prompt.read_bytes().decode("utf-8")
        if args.system_prompt
        else prompt.system_prompt
    )
    version = args.prompt_version or prompt.prompt_version
    session = uuid.uuid4().hex
    report: dict[str, Any] = {
        "session": session,
        "mode": args.mode,
        "split": args.split,
        "dataset_version": manifest["dataset_version"],
        "dataset_hash": sha256_of(documents),
        "annotations_hash": sha256_of(manifest),
        "review_state": manifest["review_state"],
        "release_ready": False,
        "prompt_version": version,
        "system_prompt_hash": sha256_text(system),
        "response_schema_hash": sha256_text(compact_json(response_json_schema())),
        "system_prompt_bytes": len(system.encode("utf-8")),
        "limitations": [
            "Synthetic cases do not establish production precision.",
            "Explanation support and usefulness require independent human review.",
            "Caps above max_actual_batch have not been capacity-tested.",
            "No automatic promotion; representative data, uncertainty and adjudication are required.",
        ],
        "trials": [],
    }
    baseline = None
    if args.compare:
        baseline = json.loads(args.compare.read_bytes())
        if any(
            baseline[key] != report[key] for key in ("dataset_hash", "annotations_hash", "split")
        ):
            parser.error(
                "baseline and candidate must have identical documents, annotations and split"
            )
        if not baseline["trials"]:
            parser.error("baseline has no trials")
    client: LlmClient = OpenAiLlmClient(config.llm) if args.mode == "live" else FakeLlmClient()
    report["model_version"] = client.model_version
    output = args.out or ROOT / "out/evaluations" / session / "summary.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    for cap, representation, order, repeat in product(
        args.caps, args.representations, args.orders, range(args.repeats)
    ):
        settings = {
            "cap": cap,
            "representation": representation,
            "order": order,
            "repeat": repeat,
            "dataset_hash": report["dataset_hash"],
            "annotations_hash": report["annotations_hash"],
            "split": args.split,
            "model_deployment": config.llm.model,
            "max_completion_tokens": config.llm.max_completion_tokens,
        }
        scope = sha256_of([session, settings])
        kwargs: dict[str, Any] = {
            "alerts": [case.alert for case in cases],
            "client": client,
            "system_prompt": system,
            "run_id": scope,
            "prompt_version": version,
            "model_version": client.model_version,
            "now": datetime.now(UTC),
            "clock": lambda: datetime.now(UTC),
            "max_batch_size": cap,
            "factored": representation == "factored",
            "reverse_order": order == "reverse",
            "audit_settings": settings,
        }
        if args.database:
            with connect(config.sql, args.database) as db:
                result = assess_alerts(
                    **kwargs, journal=SqlLlmJournal(db, scope, kind="evaluation")
                )
        else:
            result = assess_alerts(**kwargs)
        trial = {"scope_id": scope, "settings": settings, **score_trial(cases, result)}
        if report["trials"]:
            trial["comparison_to_first"] = compare_trials(report["trials"][0], trial)
        report["trials"].append(trial)
        print(
            json.dumps(
                {
                    "scope_id": scope,
                    "cases": len(cases),
                    "cap": cap,
                    "max_actual_batch": trial["max_actual_batch"],
                    "failed_attempts": trial["failed_attempts"],
                }
            )
        )
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if trial["failed_attempts"] and not args.continue_after_failure:
            report["stopped_early"] = (
                "failed attempt; inspect audit before continuing capacity trials"
            )
            break
    if baseline is not None:
        report["baseline_comparison"] = compare_trials(baseline["trials"][0], report["trials"][0])
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Evaluation summary: {output.resolve()} (release_ready=false)")
    return 1 if report.get("stopped_early") else 0


if __name__ == "__main__":
    raise SystemExit(main())
