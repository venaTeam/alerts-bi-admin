# Bounded review reads — accepted 2026-10-13

The product owner authorized performance recommendations 1, 2, 5, 7 and 8 for up to
100 teams, weekly runs and six months of accessible history. Teams range from dozens
to tens of thousands of identities and from hundreds to over a million events weekly.
Queue concurrency, retention policy, deployment separation and fleet UX changes are
excluded. The existing single-active-job and publication contracts remain binding.

## Read contract

Admin owns additive SQL read models populated atomically with completed run facts.
Existing runs are backfilled from stored facts without Elasticsearch or model calls.
Views prefixed portal_ expose only currently published completed results. Summaries
load aggregates and bounded top examples; exact full documents remain available on
detail and CSV routes. Samples never stand in for totals. Schema counts remain separate,
distinct unions remain exact and missing measurement remains NULL.

Application daily facts are captured from the completed stream, before event information
is discarded. Historical data that cannot reconstruct a metric remains explicitly
unavailable. Multi-application selections combine disjoint application identities;
overlapping rule counts are never added to infer a flagged-event union.

Web processes own bounded connection pools disposed at application shutdown. Execution,
migration and reset paths retain dedicated unpooled sessions, including session locks.
Publication visibility is checked on every request; cached private/admin results cannot
make unpublished or withdrawn data visible. Deep list navigation uses stable bounded
queries while existing bookmarked page URLs remain readable.

## Reuse contract

The scheduler may reuse an already completed healthy run with the exact deterministic
input identity. Publication order, health checks, holds and review notes are unchanged.
Unhealthy results still receive the existing assessment retry policy. Resumable analysis
must be tied to exact run/version inputs, remain private until atomic completion, and
never expose incomplete scans or change model retry/audit semantics.

Weekly executions and durable manual jobs checkpoint completed deterministic analysis in
private `run_analysis_checkpoints` rows. The versioned compressed JSON contains identity
facts, representative/sample evidence, daily counts and finalized suppression outcomes;
it never contains an executable object serialization. The run identity covers registry,
window, model, rules and application versions; a separate hash covers ES URL/index names,
parser version and checkpoint format. Fresh synchronous manual runs still scan ES.
Healthy atomic persistence discards its retry checkpoint. Explicit run deletion includes
the checkpoint. Failed/interrupted scans never create one. This is execution-state cleanup,
not a historical-data retention policy.

Migration 012 adds and backfills summary JSON, narrow list rows, rule membership and
v1-rule hashes, with application day facts captured on new executions. Migration 013 adds
private retry checkpoints. Backfill processes runs individually within the migration
transaction: rehearse upgrade duration, log growth and blocking on a representative copy
before scheduling production migration. Neither migration removes historical facts.
Shared 0.1.4 and operations 0.2.1 are the coordinated library versions. Portal readiness
checks all eleven published-only views. Unpublish/replace is visible immediately because
every request rechecks SQL publication views; only decoded immutable JSON is cached, at
most 64 entries of 256 KiB each. Evidence stays on detail/page reads and CSV exports.

New cursor links bind navigation to the run and filters and use SQL seek predicates;
the ordinary run/publication predicates apply independently of the cursor. Existing
numbered-page bookmarks remain supported. Equal event counts preserve identity ordering.
Application-filtered charts use exact stored daily unions when available; old weeks
without those facts continue to show an unavailable measurement.

## Verification and rollout

Compare stored summaries and filtered combinations against complete-input calculations.
Exercise published, withdrawn, replaced, empty, unmeasured, duplicate and historical runs,
plus weekly catch-up and failed assessment. Verify both applications in a browser.
Use real SQL Server integration and the existing critical acceptance fixture.

The scale harness must vary distinct identities independently of event count, include
100 teams and at least 27 weeks, and report cold/warm response timing, concurrency,
rows returned, memory and database storage. Initial p95 targets: 500 ms directory/overview,
750 ms findings/filter, 500 ms acknowledgement and 1.5 s visible main browser content.
Report actual environment and observations; never substitute a small fixture for proof
at the full target. Test ingestion alongside reads and preserve existing test databases.

Deliver additive schema/read-model production first, then matching shared and portal
consumers. Never edit applied migration bytes. Retain old facts/views for rollback;
revert consumers together if needed, preserving the additive data. No production
database setting, migration, deployment or data removal is authorized by implementation.
