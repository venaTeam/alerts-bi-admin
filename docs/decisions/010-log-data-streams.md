# Log data streams — accepted 2026-10-25

On 2026-10-25 the product owner asked whether the collector of decision 009 writes to an index
with the day's date. It did: `<prefix>-<stream>-YYYY.MM.DD`, the UTC day the collector received
the record. They asked that the logs go to one specific index per stream instead, and said they
will define data streams in Elasticsearch that separate the indices by date. The collector now
writes `<LOGS_INDEX_PREFIX>-runs`, `<LOGS_INDEX_PREFIX>-admin` and `<LOGS_INDEX_PREFIX>-portal`
with create operations. The prefix setting, the three streams, the document, buffering, retries
and deterministic ids of decision 009 are unchanged, though the ids now guard against
duplicates only within one backing index (below). See design section "Log shipping to
Elasticsearch" for the authoritative behavior.

Consequences: the data streams must exist (an index template with `data_stream` whose pattern
is exactly each name, since a wildcard would also match the daily names) before the collector
writes, or Elasticsearch creates plain indices under those names, which must be removed before
the data streams can take them; the writing account needs `create_doc` and `auto_configure` on
the three names, which grants on `<prefix>-<stream>-*` do not cover; the backing index a record
lands in follows the data stream's rollover, not the record's own day; the deterministic ids
prevent duplicates only within one backing index, so a chunk still being retried when its data
stream rolls over can store records again, and ids are no exactly-once guarantee (amended
2026-10-25 after review); and the daily indices already written stay as they are, unmigrated.

Admin owns the collector configuration and its runbook, including moving a deployed collector
(section 15.8); the portal's documentation names its data stream. No library or application
code changes. Merge design, admin and portal together; deploy only with separate authorization.
