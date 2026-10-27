# Parallel runs across teams — accepted 2026-10-27

On 2026-10-27 the product owner asked for parallel runs in the run worker. Asked which runs may
overlap, they chose different teams at the same time but never two weeks of one team, so one
team's backfill still runs oldest week first while other teams no longer wait behind it. Asked
for the default, they chose a setting, `WORKER_CONCURRENCY`, whose default of 1 keeps the
one-at-a-time worker until the deployment raises it. See design section "Parallel runs across
teams" for the authoritative behavior.

The assistant chose, and this records, how it is built: one worker process runs the jobs on
threads, each on its own SQL connection, and keeps the shared execution locks while any job
runs, so the one-replica deployment, the meaning of those locks for CLI runs, reports, the
weekly schedule and deletion, and crash recovery stay as they were; runs store their results
one at a time under one SQL lock, because the scan and model calls are the long part and
overlapping persistence risks cache collisions and lock escalation; and the setting is capped
at 8. The alternatives considered were several worker processes or pods (rejected: crash
recovery relies on one worker owning the execution lock) and parallel weeks of one team
(rejected by the product owner).

Admin owns the worker, migration 021, the storage lock and the deployment setting. Portal
views, shared/operations interfaces, CSV outputs and deterministic identifiers are unchanged.
Merge design, then admin; deploy only with separate authorization. Rolling back needs no schema
change: set the setting to 1, or deploy an earlier image, after the running jobs finish.
