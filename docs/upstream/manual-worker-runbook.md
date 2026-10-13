# Manual worker incident and rollback runbook

Use this when a manual job remains running beyond the deployment's normal analysis duration,
the worker disappears, or a UI/worker rollout must be reversed. Commands are examples for an
authorized operator; none have been executed on a cluster by this source change.

## Detect and inspect

Configure the existing monitoring system to alert on age of running jobs and time pending.
Choose a threshold from measured team volume and model throughput; 30 minutes is an initial
example to investigate, not an automatic cancellation deadline. Authenticated
GET /run-jobs/{job_id} returns started_at, submitted_at, status and the safe error summary.
SQL monitoring can inspect state without acquiring application locks:

```sql
SELECT job_id, team_id, run_id, status, submitted_at, started_at,
       DATEDIFF(SECOND, COALESCE(started_at, submitted_at), SYSUTCDATETIME()) AS age_seconds
FROM run_jobs WHERE active_slot = 1;
```

Inspect the run-worker container's restart count and recent run.job_submitted,
run.job_started, run.job_finished, run.job_failed and run.export_recovery_failed events.
Check ES/SQL/model availability and positive finite ES_REQUEST_TIMEOUT_MS, LLM_TIMEOUT_MS
and SQL_REQUEST_TIMEOUT_MS settings. SQL's millisecond value is converted to whole seconds:
use at least 1000 so it does not become the driver's unlimited zero timeout. Defaults are
60 seconds for SQL/ES and 120 seconds for model requests. A run can contain many requests and
three model attempts per batch, so these are not a whole-job deadline or heartbeat.

## Recover a hung or lost worker

1. Identify the job and owning worker before acting. Do not manually clear active_slot or
   mark the job failed while its worker owns the SQL execution locks: it may still commit.
2. Under incident-change authorization, restart the single admin Deployment using its
   Recreate strategy (`kubectl rollout restart deployment/alerts-bi-admin -n <namespace>`).
   This briefly interrupts the UI and terminates the old worker before its replacement runs.
3. Verify the old SQL session released alerts-bi-manual-run and alerts-bi-weekly. If SQL still
   holds the session after process termination, ask the database operator to investigate and
   terminate the confirmed abandoned session; do not kill unrelated database sessions.
4. The replacement worker reconciles running jobs only after acquiring those locks. A result
   committed by this attempt has all CSVs regenerated and becomes completed. CSV regeneration
   failure remains completed with a visible export warning. An uncommitted attempt becomes
   failed with an interruption message. Pending work proceeds normally.
5. Confirm status, CSV bytes and unpublished result. Retry failed analysis with a new key;
   retrying the previous key returns that same failed attempt. Regenerate warned exports with
   `alerts-bi-admin report --run-id <id> --database <database> --out <explicit-run-directory>`.

## Rollback with durable submissions

Drain pending/running work before a planned rollback. If that is impossible, stop the old
UI and worker together, confirm their SQL sessions have released the application locks,
then run the current compatible worker once to reconcile interrupted commits. Pending jobs
can be completed with the current worker before rollback, or explicitly failed by the database
operator after all writers are stopped and locked-session checks have passed:

```sql
UPDATE run_jobs
SET status = 'failed', active_slot = NULL, completed_at = SYSUTCDATETIME(),
    error_summary = N'Cancelled during controlled rollback. Start a new run to retry.'
WHERE status = 'pending';
```

Use the compatible worker for running jobs; the pending-only statement deliberately cannot
discard a committed analysis or race a live worker. Record affected job IDs in the incident
audit. Never downgrade, edit or stamp over migrations 001–011. Preserve submission records,
CSV data and committed analyses. After 011, old writers that omit idempotency_key_hash cannot
accept new jobs: roll forward or use a compatible writer, rather than deploying pre-011 code.
