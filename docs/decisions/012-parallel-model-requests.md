# Parallel model requests within a run — accepted 2026-10-27

On 2026-10-27 the product owner asked for parallel model requests within one run, alongside
parallel runs across teams (decision 011). Asked whether the limit should apply per run or as
one total shared by all of the worker's jobs, they chose per run: a setting, `LLM_CONCURRENCY`,
from 1 to 8, whose default of 1 keeps sending one batch at a time until the deployment raises
it. They accepted that the endpoint can then see up to `WORKER_CONCURRENCY` times
`LLM_CONCURRENCY` requests at once. See design section "Parallel model requests within a run"
for the authoritative behavior.

The assistant chose, and this records, how it is built: the run's batches go to a thread pool
of that size; each batch keeps its own three attempts and retry waits; the audit journal stays
on the run's one SQL connection, with its short writes taken one at a time under a lock, so the
pre-request audit entry, the run's session lock and crash recovery stay as they were; and each
batch's results are combined in batch order, so stored rows and identifiers match a run at 1.
The setting is left out of the run's recorded model settings because it does not change any
batch. The alternatives considered were one worker-wide total (rejected by the product owner:
one large run could take most of the requests while other teams' runs wait), a SQL connection
per request thread (rejected: the session lock and journal would no longer share one
connection, for no gain while the requests dominate), and a shared back-off across batches
after a `429` (not needed for this change; each batch already honours its own `Retry-After`).

Admin owns the setting, the assessment change and the deployment examples. Portal views,
shared/operations interfaces, migrations, CSV outputs and deterministic identifiers are
unchanged. Merge after decision 011's change, design first, then admin; deploy only with
separate authorization. Rolling it back is setting `LLM_CONCURRENCY=1` and restarting.
