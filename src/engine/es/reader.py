"""Team-scoped Elasticsearch retrieval (design section 6; flow step 2).

Two separate queries, each restricted to the selected team's configured operators and to
the exact run window. The queries do not restrict ``application``, do not apply the team's
own panel filters, do not read the SQL hot table, and never sweep another team's alerts.

Elasticsearch row ids are read but never persisted as business identity: source documents
expire after three months, so an id is not a durable reference.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter, sleep
from typing import Any

from alerts_bi_operations.registry import TeamEntry
from alerts_bi_shared.logging_setup import log, redact_error
from alerts_bi_shared.timefmt import iso_instant
from alerts_bi_shared.window import RunWindow

from ..domain.normalize import AlertRecord, normalize_row
from .client import ElasticsearchError, EsClient

__all__ = [
    "OPERATOR_FIELD",
    "ReadResult",
    "build_query",
    "read_schema",
    "read_team_alerts",
    "scan_schema",
]


# Transport failures (no HTTP status) and server-side statuses that are worth another try.
# Anything else (a bad query, a missing or expired point-in-time) cannot succeed on retry.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# The v1/v2 indices map ``operator`` as analyzed ``text`` with a ``keyword`` subfield. A
# ``terms`` query on the analyzed field compares registry values with lowercased tokens, so
# ``893_Lannister`` would silently match nothing; only the subfield holds the exact value.
OPERATOR_FIELD = "operator.keyword"


@dataclass(frozen=True, slots=True)
class ReadResult:
    rows: list[AlertRecord]
    pages: int
    reported_total: int
    """Total hits Elasticsearch reported, used as a paging check."""


def build_query(operators: Sequence[str], window: RunWindow) -> dict[str, Any]:
    """Build the query body for one schema.

    The window is the exact half-open range: ``gte`` window_start, ``lt`` window_end.
    Using ``lte`` would double-count the boundary instant across two adjacent runs.

    ``terms`` on the ``operator.keyword`` subfield matches the exact, case-sensitive values
    from the registry - which is what makes ``Checkout-API`` and ``checkout`` two distinct
    entries a team must list separately.
    """
    return {
        "bool": {
            "filter": [
                {"terms": {OPERATOR_FIELD: list(operators)}},
                {
                    "range": {
                        "@timestamp": {
                            "gte": _iso(window.window_start),
                            "lt": _iso(window.window_end),
                            "format": "strict_date_optional_time",
                        }
                    }
                },
            ]
        }
    }


def _iso(value: datetime) -> str:
    """Render an instant the way Elasticsearch date parsing expects."""
    return iso_instant(value)


def _search_with_retry(client: EsClient, body: dict[str, Any], index: str) -> dict[str, Any]:
    """Run one page search, retrying transient failures with exponential backoff.

    Re-sending the identical body is safe: the PIT pins one view of the index and
    ``search_after`` pins the page, so a retry returns exactly the page that failed. Rows
    are only consumed after a successful response, so a retry cannot duplicate rows.
    """
    config = client.config
    attempt = 0
    while True:
        try:
            return client.search(body)
        except ElasticsearchError as exc:
            if (
                exc.status is not None and exc.status not in _RETRYABLE_STATUSES
            ) or attempt >= config.search_retries:
                raise
            delay = config.search_retry_backoff_ms / 1000 * 2**attempt
            attempt += 1
            log.warn(
                "es.search_retry",
                index=index,
                attempt=attempt,
                max_retries=config.search_retries,
                status=exc.status,
                delay_seconds=round(delay, 3),
                error=redact_error(exc),
            )
            sleep(delay)


def read_schema(
    client: EsClient,
    schema: str,
    operators: Sequence[str],
    window: RunWindow,
    page_size: int | None = None,
) -> ReadResult:
    """Materializing adapter for small reads/tests. Production uses ``scan_schema``."""
    rows: list[AlertRecord] = []
    pages, total = scan_schema(client, schema, operators, window, rows.append, page_size)
    return ReadResult(rows, pages, total)


def scan_schema(
    client: EsClient,
    schema: str,
    operators: Sequence[str],
    window: RunWindow,
    consume: Callable[[AlertRecord], None],
    page_size: int | None = None,
    *,
    hash_documents: bool = True,
) -> tuple[int, int]:
    """Consume one page at a time, returning (pages, exact total) only after validation.

    Timestamp order is required by the incremental firing analysis. Consumers must not
    publish partial results: count reconciliation happens after the last page. The PIT
    is closed even if consumption fails. No callback retains a response page here.
    """
    if schema not in {"v1", "v2"}:
        raise ValueError(f"unknown alert schema: {schema!r}")
    index = client.config.v1_index if schema == "v1" else client.config.v2_index
    size = page_size if page_size is not None else client.config.page_size
    if size <= 0:
        raise ValueError("Elasticsearch page size must be positive")

    if not operators:
        # Not an error: a migrated team has no v1 operators, and a pre-migration team has
        # no v2 operator. Querying with an empty terms list would match nothing anyway,
        # but skipping makes the intent explicit in the logs.
        log.info("es.skip_schema", schema=schema, reason="no configured operators")
        return 0, 0

    query = build_query(operators, window)
    pit_id = client.open_pit(index)
    row_count = 0
    pages = 0
    reported_total = 0
    search_after: list[Any] | None = None
    started = last_progress = perf_counter()
    search_seconds = normalize_seconds = consume_seconds = 0.0
    cluster_ms = 0

    try:
        while True:
            body: dict[str, Any] = {
                "size": size,
                "track_total_hits": pages == 0,
                "allow_partial_search_results": False,
                "query": query,
                # _shard_doc is the PIT tiebreaker: it guarantees a total order, so no
                # document is skipped or repeated across pages even when timestamps
                # collide.
                "sort": [{"@timestamp": "asc"}, {"_shard_doc": "asc"}],
                "pit": {"id": pit_id, "keep_alive": client.config.pit_keep_alive},
            }
            if search_after is not None:
                body["search_after"] = search_after

            before = perf_counter()
            response = _search_with_retry(client, body, index)
            search_seconds += perf_counter() - before
            cluster_ms += int(response.get("took", 0))
            pit_id = response.get("pit_id", pit_id)
            if response.get("timed_out") or response.get("_shards", {}).get("failed", 0):
                raise ElasticsearchError(f"incomplete search response for {index}")
            hits = response.get("hits", {}).get("hits", [])
            if pages == 0:
                total = response.get("hits", {}).get("total", {})
                if total.get("relation") != "eq" or "value" not in total:
                    raise ElasticsearchError(f"{index}: first page has no exact total")
                reported_total = int(total["value"])
            pages += 1

            for hit in hits:
                before = perf_counter()
                row = normalize_row(schema, hit["_source"], hash_document=hash_documents)
                normalize_seconds += perf_counter() - before
                before = perf_counter()
                consume(row)
                consume_seconds += perf_counter() - before
                row_count += 1

            now = perf_counter()
            if now - last_progress >= 30:
                log.info(
                    "es.read_progress",
                    schema=schema,
                    rows=row_count,
                    total=reported_total,
                    pages=pages,
                    elapsed_seconds=round(now - started, 3),
                )
                last_progress = now

            if len(hits) < size:
                break
            search_after = hits[-1].get("sort")
            if not search_after:
                raise ElasticsearchError(
                    f"page {pages} of {index} returned no sort values, "
                    "so paging cannot continue safely"
                )
    finally:
        client.close_pit(pit_id)

    # A mismatch means paging lost or duplicated rows, which would silently corrupt every
    # number downstream. Fail rather than publish a partial team scorecard as complete.
    if row_count != reported_total:
        raise ElasticsearchError(
            f"{index}: retrieved {row_count} rows but the cluster reported "
            f"{reported_total} matching"
        )

    log.info(
        "es.read_schema",
        schema=schema,
        index=index,
        operators=len(operators),
        rows=row_count,
        pages=pages,
        search_seconds=round(search_seconds, 3),
        cluster_seconds=round(cluster_ms / 1000, 3),
        normalize_seconds=round(normalize_seconds, 3),
        consume_seconds=round(consume_seconds, 3),
        elapsed_seconds=round(perf_counter() - started, 3),
    )
    return pages, reported_total


def read_team_alerts(
    client: EsClient,
    team: TeamEntry,
    window: RunWindow,
    page_size: int | None = None,
) -> dict[str, ReadResult]:
    """Read both schemas for one selected team."""
    v2_operators = [] if team.v2_operator is None else [team.v2_operator]
    return {
        "v1": read_schema(client, "v1", team.v1_operators, window, page_size),
        "v2": read_schema(client, "v2", v2_operators, window, page_size),
    }
