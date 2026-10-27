"""LLM assessment orchestration (design section 5.1; flow step 6).

Only identities with no core finding on any row enter this stage; alerts carrying v2
readiness gaps are still eligible. Coverage is exhaustive - there is no classification
budget - so ``unassessed`` is a failure state rather than a design parameter, and any
non-zero value reads as a broken classifier.
"""

from __future__ import annotations

import contextvars
import json
import random
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any, Final

from alerts_bi_shared.hashing import compact_json
from alerts_bi_shared.logging_setup import log, log_context, redact_error
from alerts_bi_shared.versions import RULESET_VERSION

from ..db.llm_audit import SqlLlmJournal
from ..db.repositories import verdict_key
from ..domain.normalize import AlertRecord
from .client import Completion, LlmClient, LlmTransportError
from .grouping import Batch, build_batches, reverse_for_evaluation
from .request import assert_lossless, build_request, serialize_request
from .response import (
    LlmResponseError,
    Verdict,
    state_for_verdict,
    validate_response,
)

__all__ = [
    "MAX_ATTEMPTS",
    "MAX_RETRY_AFTER_SECONDS",
    "RETRY_DELAYS_SECONDS",
    "AssessmentOutcome",
    "AssessmentResult",
    "assess_alerts",
    "mark_all_unassessed",
]

#: The initial request plus two retries. Not configurable.
MAX_ATTEMPTS: Final = 3

#: Seconds to wait after the first and second failed live attempts, before the retry. A
#: failed call is often a rate limit or an overloaded endpoint, which an immediate resend
#: only repeats. Not configurable, like the attempt count.
RETRY_DELAYS_SECONDS: Final = (2.0, 5.0)

#: Up to this fraction of the delay is added at random, so concurrent runs do not retry in
#: lockstep.
_RETRY_JITTER: Final = 0.25

#: Ceiling for an endpoint's ``Retry-After``: a run must not stall on a hostile or
#: misconfigured header.
MAX_RETRY_AFTER_SECONDS: Final = 30.0


@dataclass(frozen=True, slots=True)
class AssessmentOutcome:
    state: str
    """``llm_flagged`` | ``needs_review`` | ``assessed_good`` | ``unassessed``"""
    principle_id: str | None
    confidence: str | None
    justification: str | None
    unassessed_reason: str | None
    reused: bool
    """True when a durable verdict was reused rather than requested."""
    alert: AlertRecord


@dataclass(slots=True)
class AssessmentResult:
    outcomes: dict[str, AssessmentOutcome] = field(default_factory=dict)
    """Keyed by alert identity."""
    batch_attempts: list[dict[str, Any]] = field(default_factory=list)
    new_verdicts: list[dict[str, Any]] = field(default_factory=list)
    requested_batches: int = 0
    reused_verdicts: int = 0


def _naive(value: datetime) -> datetime:
    """SQL Server DATETIME2 columns take naive UTC values through this driver."""
    return value.replace(tzinfo=None)


def assess_alerts(
    *,
    alerts: Sequence[AlertRecord],
    client: LlmClient,
    system_prompt: str,
    run_id: str,
    prompt_version: str,
    model_version: str,
    now: datetime,
    max_batch_size: int | None = None,
    existing_verdicts: Mapping[str, Mapping[str, Any]] | None = None,
    journal: SqlLlmJournal | None = None,
    clock: Callable[[], datetime] | None = None,
    audit_settings: dict[str, Any] | None = None,
    factored: bool = True,
    reverse_order: bool = False,
    sleep: Callable[[float], None] | None = None,
    concurrency: int = 1,
) -> AssessmentResult:
    """Assess eligible identities; live requests require durable pre-call SQL audit.

    ``sleep`` waits between a failed live attempt and its retry. By default a client that
    needs the audit journal (a real endpoint) waits with ``time.sleep``; any other client
    never touches a network, so it does not wait.

    ``concurrency`` is how many batches wait on the model at once (``LLM_CONCURRENCY``).
    It changes no batch and no result, so it is not among the audited settings.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if getattr(client, "requires_audit", False) and journal is None:
        raise ValueError("live LLM assessment requires a SQL audit journal")
    if (not factored or reverse_order) and journal is not None and journal.kind != "evaluation":
        raise ValueError("representation and ordering trials require an evaluation scope")
    with journal.locked() if journal is not None else nullcontext():
        if journal is not None:
            journal.configure(
                system_prompt,
                prompt_version,
                model_version,
                {
                    **(audit_settings or {}),
                    "max_batch_size": max_batch_size or 200,
                    "factored": factored,
                    "reverse_order": reverse_order,
                },
            )
        return _assess_alerts(
            alerts=alerts,
            client=client,
            system_prompt=system_prompt,
            run_id=run_id,
            prompt_version=prompt_version,
            model_version=model_version,
            now=now,
            max_batch_size=max_batch_size,
            existing_verdicts=existing_verdicts,
            journal=journal,
            clock=clock or (lambda: now),
            factored=factored,
            reverse_order=reverse_order,
            sleep=sleep
            if sleep is not None
            else (time.sleep if getattr(client, "requires_audit", False) else _no_sleep),
            concurrency=concurrency,
        )


def _no_sleep(_seconds: float) -> None:
    return None


def _assess_alerts(
    *,
    alerts: Sequence[AlertRecord],
    client: LlmClient,
    system_prompt: str,
    run_id: str,
    prompt_version: str,
    model_version: str,
    now: datetime,
    max_batch_size: int | None = None,
    existing_verdicts: Mapping[str, Mapping[str, Any]] | None = None,
    journal: SqlLlmJournal | None,
    clock: Callable[[], datetime],
    factored: bool,
    reverse_order: bool,
    sleep: Callable[[float], None],
    concurrency: int = 1,
) -> AssessmentResult:
    """Assess every eligible alert."""
    existing = existing_verdicts or {}
    result = AssessmentResult()

    # 6.1 Reuse durable verdicts first. A stored verdict belongs to the prompt and model
    # version that produced it and is never recomputed under the same pair.
    to_request: list[AlertRecord] = []
    for alert in alerts:
        stored = existing.get(verdict_key(alert.application, alert.key_field))
        if stored is None:
            to_request.append(alert)
            continue
        result.outcomes[alert.identity] = AssessmentOutcome(
            state=state_for_verdict(stored),
            principle_id=str(stored["principle_id"]),
            confidence=str(stored["confidence"]),
            justification=str(stored["justification"]),
            unassessed_reason=None,
            reused=True,
            alert=alert,
        )
        result.reused_verdicts += 1

    batches = build_batches(to_request, run_id, prompt_version, model_version, max_batch_size)
    if reverse_order:
        batches = [
            reverse_for_evaluation(batch, run_id, prompt_version, model_version)
            for batch in batches
        ]
    result.requested_batches = len(batches)
    progress = _Progress(len(batches))
    log.info(
        "llm.started",
        "Assessing {alert_count:,} alerts in {batches} {batch_word}; {reused:,} verdicts reused "
        "from earlier runs"
        if batches
        else "Nothing to send to the model; {reused:,} verdicts reused from earlier runs",
        batch_word="batch" if len(batches) == 1 else "batches",
        alert_count=len(to_request),
        batches=len(batches),
        reused=result.reused_verdicts,
        model=model_version,
        max_batch_size=max_batch_size or 200,
    )

    stop = threading.Event()
    assess = partial(
        _assess_batch,
        batch_count=len(batches),
        client=client,
        system_prompt=system_prompt,
        run_id=run_id,
        prompt_version=prompt_version,
        now=now,
        journal=journal,
        clock=clock,
        factored=factored,
        sleep=sleep,
        stopping=stop.is_set,
    )
    finished: list[_BatchOutcome | None] = [None] * len(batches)
    if concurrency == 1 or len(batches) < 2:
        for index, batch in enumerate(batches):
            finished[index] = ended = assess(index + 1, batch)
            progress.record(ended.attempts, succeeded=ended.verdicts is not None)
    else:
        # Only the requests and retry waits overlap; the journal serves one thread at a
        # time. Each task gets its own copy of the run's log context.
        with ThreadPoolExecutor(
            max_workers=min(concurrency, len(batches)), thread_name_prefix="llm-batch"
        ) as pool:
            futures = {
                pool.submit(contextvars.copy_context().run, assess, index + 1, batch): index
                for index, batch in enumerate(batches)
            }
            try:
                for future in as_completed(futures):
                    finished[futures[future]] = ended = future.result()
                    progress.record(ended.attempts, succeeded=ended.verdicts is not None)
            except BaseException:
                # Start no further batch and no further attempt of one in flight; leaving
                # the block waits for each request already sent, so its audit record is
                # complete before the error propagates.
                stop.set()
                for future in futures:
                    future.cancel()
                raise

    # Combined in batch order, so what the run stores does not depend on which request
    # answered first.
    for done in finished:
        assert done is not None
        batch, verdicts = done.batch, done.verdicts
        result.batch_attempts.extend(done.attempts)
        if verdicts is not None:
            for index, verdict in enumerate(verdicts):
                alert = batch.alerts[index]
                result.outcomes[alert.identity] = AssessmentOutcome(
                    state=state_for_verdict(verdict),
                    principle_id=verdict.principle_id,
                    confidence=verdict.confidence,
                    justification=verdict.justification,
                    unassessed_reason=None,
                    reused=False,
                    alert=alert,
                )
                result.new_verdicts.append(
                    {
                        "application": alert.application,
                        "key_field": alert.key_field,
                        "prompt_version": prompt_version,
                        "model_version": model_version,
                        "alert_schema": alert.schema,
                        "assessment": verdict.assessment,
                        "principle_id": verdict.principle_id,
                        "confidence": verdict.confidence,
                        "justification": verdict.justification,
                        # The source row expires after three months, so the exact document
                        # the model judged is stored here or the verdict is unauditable.
                        "representative_doc": compact_json(alert.source),
                        "doc_hash": alert.doc_hash,
                        "classified_at": _naive(done.classified_at),
                        "ruleset_version": RULESET_VERSION,
                        "first_run_id": run_id,
                    }
                )
        else:
            # After the third failure, EVERY alert in the batch becomes unassessed with the
            # shared reason. Alerts are never retried individually.
            for alert in batch.alerts:
                result.outcomes[alert.identity] = AssessmentOutcome(
                    state="unassessed",
                    principle_id=None,
                    confidence=None,
                    justification=None,
                    unassessed_reason=done.failure_reason,
                    reused=False,
                    alert=alert,
                )

    progress.finish()
    return result


class _Stopping(Exception):
    """The run is failing: a request thread starts no further attempt of its batch.

    An attempt already sent finishes and is recorded; the batch resumes from the audit
    journal when the run is retried.
    """


@dataclass(frozen=True, slots=True)
class _BatchOutcome:
    """How one batch ended, kept until every batch of the run has."""

    batch: Batch
    attempts: list[dict[str, Any]]
    verdicts: list[Verdict] | None
    failure_reason: str
    classified_at: datetime


def _assess_batch(
    position: int,
    batch: Batch,
    *,
    batch_count: int,
    client: LlmClient,
    system_prompt: str,
    run_id: str,
    prompt_version: str,
    now: datetime,
    journal: SqlLlmJournal | None,
    clock: Callable[[], datetime],
    factored: bool,
    sleep: Callable[[float], None],
    stopping: Callable[[], bool],
) -> _BatchOutcome:
    """Build, send and log one batch; runs on a request thread when batches overlap."""
    request = build_request(batch, RULESET_VERSION, prompt_version)
    if not factored:
        # Evaluation-only representation trial. Every source field is retained.
        request["shared_fields"] = {}
        for item, alert in zip(request["alerts"], batch.alerts, strict=True):
            item["fields"] = alert.source
    # Losslessness is asserted before the first call, so a factoring bug fails loudly
    # instead of quietly sending the model documents with fields missing.
    assert_lossless(request, batch)

    # Serialized ONCE and reused byte-for-byte on every retry.
    request_text, request_hash = serialize_request(request)

    attempts: list[dict[str, Any]] = []
    with log_context(batch_id=batch.batch_id):
        verdicts, failure_reason, classified_at = _attempt_batch(
            batch=batch,
            request_text=request_text,
            request_hash=request_hash,
            client=client,
            system_prompt=system_prompt,
            run_id=run_id,
            now=now,
            batch_attempts=attempts,
            journal=journal,
            clock=clock,
            sleep=sleep,
            position=(position, batch_count),
            stopping=stopping,
        )
        _log_group(batch, (position, batch_count), attempts, verdicts)
    return _BatchOutcome(batch, attempts, verdicts, failure_reason, classified_at)


def _log_group(
    batch: Batch,
    position: tuple[int, int],
    attempts: Sequence[Mapping[str, Any]],
    verdicts: Sequence[Verdict] | None,
) -> None:
    """One record per model group, with its alerts, for the runs log index.

    Each alert carries its identity and the outcome of this group: the state and, when the
    model answered, its assessment, principle and confidence. Never the alert document or
    the model's justification (design "Log shipping to Elasticsearch").
    """
    if not log.enabled("info", console=False):
        return
    alerts: list[dict[str, Any]] = []
    for index, alert in enumerate(batch.alerts):
        entry: dict[str, Any] = {
            "schema": alert.schema,
            "application": alert.application,
            "key_field": alert.key_field,
            "alert_id": batch.alert_ids[index],
        }
        if verdicts is None:
            entry["state"] = "unassessed"
        else:
            verdict = verdicts[index]
            entry["state"] = state_for_verdict(verdict)
            entry["assessment"] = verdict.assessment
            entry["principle_id"] = verdict.principle_id
            entry["confidence"] = verdict.confidence
        alerts.append(entry)
    states = dict.fromkeys(("llm_flagged", "needs_review", "assessed_good", "unassessed"), 0)
    for entry in alerts:
        states[entry["state"]] = states.get(entry["state"], 0) + 1
    tokens: dict[str, Any] = {}
    for key in ("input_tokens", "cached_tokens", "output_tokens"):
        values = [attempt.get("metadata", {}).get(key) for attempt in attempts]
        if values and all(isinstance(value, int) for value in values):
            tokens[f"tokens_{key.removesuffix('_tokens')}"] = sum(values)
    part = (
        f", part {batch.partition_index + 1} of {batch.partition_count}"
        if batch.partition_count > 1
        else ""
    )
    log.info(
        "llm.group",
        "Group {group} of {groups} ({group_type} {group_value}{part}): {alert_count} "
        "alert{alert_count:plural} "
        + (
            "assessed on attempt {attempts} in {duration_ms:ms}: {flagged} flagged, "
            "{needs_review} need review, {good} good"
            if verdicts is not None
            else "failed all {attempts} attempts in {duration_ms:ms}; they stay unassessed"
        ),
        console=False,
        group=position[0],
        groups=position[1],
        group_type=batch.group_type,
        group_value=batch.group_value,
        part=part,
        partition=batch.partition_index + 1,
        partitions=batch.partition_count,
        alert_count=len(alerts),
        attempts=len(attempts),
        status="succeeded" if verdicts is not None else "exhausted",
        duration_ms=sum(int(attempt.get("duration_ms") or 0) for attempt in attempts),
        flagged=states["llm_flagged"],
        needs_review=states["needs_review"],
        good=states["assessed_good"],
        unassessed=states["unassessed"],
        **tokens,
        alerts=alerts,
    )


class _Progress:
    """Batch counts for a progress record every 30 seconds and a closing summary."""

    INTERVAL_SECONDS: Final = 30.0

    def __init__(self, total: int) -> None:
        self.total = total
        self.done = 0
        self.retried = 0
        self.exhausted = 0
        self.tokens: dict[str, int | None] = {
            "input_tokens": 0,
            "cached_tokens": 0,
            "output_tokens": 0,
        }
        self.started = self.last = time.monotonic()

    def record(self, attempts: Sequence[Mapping[str, Any]], *, succeeded: bool) -> None:
        self.done += 1
        self.retried += len(attempts) > 1
        self.exhausted += not succeeded
        for attempt in attempts:
            metadata = attempt.get("metadata") or {}
            for key, total in self.tokens.items():
                value = metadata.get(key) if isinstance(metadata, Mapping) else None
                # Usage the endpoint did not report stays unknown, never zero.
                self.tokens[key] = (
                    None if total is None or not isinstance(value, int) else total + value
                )
        now = time.monotonic()
        if now - self.last >= self.INTERVAL_SECONDS and self.done < self.total:
            self.last = now
            log.info(
                "llm.progress",
                "{done} of {total} batches done ({percent}%), {retried} retried, {exhausted} "
                "exhausted, {elapsed_seconds:dur}",
                done=self.done,
                total=self.total,
                percent=round(100 * self.done / self.total),
                retried=self.retried,
                exhausted=self.exhausted,
                elapsed_seconds=round(now - self.started, 3),
            )

    def finish(self) -> None:
        if not self.total:
            return
        usage: dict[str, Any] = {
            f"tokens_{key.removesuffix('_tokens')}": value
            for key, value in self.tokens.items()
            if value is not None
        }
        log.info(
            "llm.done",
            "{total} batches in {elapsed_seconds:dur}: {retried} needed a retry, {exhausted} "
            "exhausted",
            total=self.total,
            retried=self.retried,
            exhausted=self.exhausted,
            elapsed_seconds=round(time.monotonic() - self.started, 3),
            **usage,
        )


def _attempt_batch(
    *,
    batch: Batch,
    request_text: str,
    request_hash: str,
    client: LlmClient,
    system_prompt: str,
    run_id: str,
    now: datetime,
    batch_attempts: list[dict[str, Any]],
    journal: SqlLlmJournal | None,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    position: tuple[int, int] = (1, 1),
    stopping: Callable[[], bool] = lambda: False,
) -> tuple[list[Verdict] | None, str, datetime]:
    """Run one batch through at most three attempts.

    A live attempt that fails is followed by a pause (see :func:`_retry_delay`) before the
    next live attempt; the final failure and attempts replayed from the journal do not wait.

    Each retry sends the IDENTICAL batch as a unit: same membership, same ordering, same
    bytes. There is no alert-by-alert fallback, because a verdict attached to the wrong
    alert is the worst output this system can produce.
    """
    failure_reason = "unknown failure"
    cycle = journal.prepare(batch.batch_id, request_text, request_hash) if journal else ""
    completed_at = now

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if stopping():
            raise _Stopping("another batch of this run failed; no further attempt starts")
        status = "succeeded"
        reason: str | None = None
        verdicts: list[Verdict] | None = None
        response_text: str | None = None
        metadata: dict[str, Any] = {}
        retry_after: float | None = None
        saved = journal.attempt(cycle, attempt) if journal else None
        created_at = saved["created_at"] if saved else clock()
        if journal is not None and saved is None:
            journal.start(cycle, attempt, created_at)
        # After the audit writes, so a wait for another request thread's write is not
        # counted as the request's latency.
        started = time.monotonic()

        try:
            if saved is not None:
                metadata = json.loads(saved["metadata_json"])
                if saved["status"] == "started":
                    raise LlmTransportError(
                        "previous process ended without a committed response; remote outcome unknown",
                        "interrupted",
                    )
                if saved["status"] != "succeeded":
                    raise LlmTransportError(
                        saved["failure_reason"] or "stored failed attempt", saved["status"]
                    )
                response_text = str(saved["response_text"])
            else:
                completion = client.complete(
                    system_prompt=system_prompt,
                    request_text=request_text,
                    batch_id=batch.batch_id,
                    attempt=attempt,
                )
                if isinstance(completion, Completion):
                    response_text, metadata = completion.text, completion.metadata
                else:
                    response_text = completion
            try:
                parsed = json.loads(response_text)
            except json.JSONDecodeError as exc:
                raise LlmResponseError("response is not valid JSON") from exc

            verdicts = validate_response(
                parsed, batch.batch_id, batch.alert_ids, alerts=batch.alerts
            )
        except LlmTransportError as exc:
            # A timeout or transport error consumes one recorded attempt like any other
            # failed call.
            status = (
                exc.kind
                if exc.kind
                in {
                    "timeout",
                    "refusal",
                    "incomplete",
                    "empty",
                    "interrupted",
                    "invalid_response",
                    "transport_error",
                }
                else "transport_error"
            )
            metadata = exc.metadata or metadata
            response_text = exc.response_text or response_text
            retry_after = exc.retry_after
            reason = redact_error(exc)
            failure_reason = f"{status}: {reason}"
            verdicts = None
        except LlmResponseError as exc:
            status = "invalid_response"
            reason = redact_error(exc)
            failure_reason = f"{status}: {reason}"
            verdicts = None

        completed_at = saved["completed_at"] if saved and saved["completed_at"] else clock()
        duration = (
            int(saved["duration_ms"] or 0) if saved else int((time.monotonic() - started) * 1000)
        )
        if journal is not None and (saved is None or saved["status"] == "started"):
            completed_at = journal.finish(
                cycle,
                attempt,
                status=status,
                response=response_text,
                reason=None if reason is None else reason[:1000],
                metadata=metadata,
                duration_ms=duration,
                completed_at=completed_at,
            )
        batch_attempts.append(
            {
                "run_id": run_id,
                "batch_id": batch.batch_id,
                "attempt_number": attempt,
                "group_type": batch.group_type,
                "group_value": batch.group_value,
                "partition_index": batch.partition_index,
                "partition_count": batch.partition_count,
                "alert_count": len(batch.alerts),
                "alert_ids": compact_json(list(batch.alert_ids)),
                "request_hash": request_hash,
                # The complete payload is auditable data, kept in SQL and never in logs.
                "request_payload": request_text,
                "status": status,
                "failure_reason": None if reason is None else reason[:1000],
                "duration_ms": duration,
                "created_at": _naive(created_at),
                "metadata": metadata,
            }
        )

        if verdicts is not None:
            # The group record written by the caller says how this batch ended.
            if journal is not None:
                journal.complete_batch(cycle, succeeded=True)
            return verdicts, "", completed_at

        retrying = saved is None and attempt < MAX_ATTEMPTS
        delay = _retry_delay(attempt, retry_after) if retrying else 0.0
        log.warn(
            "llm.batch_attempt_failed",
            "Batch {batch} of {batches}, attempt {attempt} of {max_attempts}: {status} after "
            "{duration_ms:ms}; {next_step}",
            batch=position[0],
            batches=position[1],
            attempt=attempt,
            max_attempts=MAX_ATTEMPTS,
            status=status,
            duration_ms=duration,
            next_step=f"retrying in {delay:.1f}s"
            if retrying
            else "replayed from the audit journal"
            if saved is not None
            else "no attempts left",
            alert_count=len(batch.alerts),
            error=reason,
        )
        if retrying:
            sleep(delay)

    log.warn(
        "llm.batch_exhausted",
        "Batch {batch} of {batches} failed all {attempts} attempts; its {alert_count} alerts stay "
        "unassessed and the run continues",
        batch=position[0],
        batches=position[1],
        attempts=MAX_ATTEMPTS,
        alert_count=len(batch.alerts),
        error=failure_reason,
    )
    if journal is not None:
        journal.complete_batch(cycle, succeeded=False)
    return None, f"batch exhausted {MAX_ATTEMPTS} attempts - {failure_reason}"[:500], completed_at


def _retry_delay(failed_attempt: int, retry_after: float | None) -> float:
    """Seconds to wait after ``failed_attempt`` (1-based) fails.

    The fixed schedule, lengthened to the endpoint's ``Retry-After`` when that is larger
    (capped), plus a little jitter.
    """
    base = RETRY_DELAYS_SECONDS[min(failed_attempt, len(RETRY_DELAYS_SECONDS)) - 1]
    if retry_after is not None:
        base = max(base, min(retry_after, MAX_RETRY_AFTER_SECONDS))
    return base * (1 + random.uniform(0, _RETRY_JITTER))


def mark_all_unassessed(alerts: Sequence[AlertRecord], reason: str) -> dict[str, AssessmentOutcome]:
    """Mark every eligible identity unassessed without calling a model.

    Used when the run is executed with LLM assessment switched off. The reason is recorded
    per identity so the scorecard never presents "we did not look" as "it is fine".
    """
    return {
        alert.identity: AssessmentOutcome(
            state="unassessed",
            principle_id=None,
            confidence=None,
            justification=None,
            unassessed_reason=reason,
            reused=False,
            alert=alert,
        )
        for alert in alerts
    }
