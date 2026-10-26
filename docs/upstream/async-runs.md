# Durable manual runs — accepted 2026-10-13

**Amended 2026-10-17:** runs no longer write CSV files (design section "Run output files
removed"). Worker execution and recovery persist to SQL only; downloads render from SQL on
each request. The export warning, recovery regeneration, `remove-run-html`, the output
settings and the output mount below are removed, and `POST /runs` drops `out_dir`.

Manual analysis can outlast an OpenShift Route timeout. The product owner requested asynchronous
execution with results displayed when complete, and removal of generated per-run HTML files.
This amendment supersedes synchronous browser triggering. The subsequent accepted request removes standalone HTML output entirely, including on-demand rendering.
The 2026-10-14 [backfill contract](backfill.md) adds the `queued` and `cancelled` job states, a
worker claim order, queueing of manual submissions while a backfill is in progress, and a weekly
wait for the current backfill week. Statements below that no queue exists apply only while no
backfill is in progress.

## HTTP and UI contract

- Browser `POST /teams/{team_id}/trigger` validates identity, same-site CSRF and run options,
  persists a submission, then immediately redirects (303) to that team's workspace with `job_id`.
- Amended 2026-10-19 (design "Admin console without login"): no route requires an operator
  identity and no requester is recorded; submission keys are unique across all clients.
- JSON `POST /run-jobs` requires same-site `X-CSRF-Token` and an
  `Idempotency-Key` (1–64 ASCII characters). It accepts the existing team/run_at/llm body and
  returns 202 with `job_id`, `team_id`, `run_id`, status/timestamps, `error_summary`,
  `status_url`, `result_url` (null until completed) and a Location pointing to status.
- `GET /run-jobs/{job_id}` returns current durable state. Unknown jobs return 404.
  The status response never exposes configuration, submission keys or full error payloads.
  Anyone who can reach the console can inspect any team's job; there is no per-team ACL. The team workspace still rejects a selected job from another team.
  The complete console HTTP schema is pinned in contracts/console-openapi.json.
- Pending/running submissions appear above the selected analysis in the team workspace.
  Same-origin polling every three seconds updates state; it pauses while the page is hidden.
  Completion of the job selected through `job_id` opens its completed run. Other selections
  retain their URL/filter state when refreshed. Returning later reads current state from SQL.
  Failed jobs offer the existing New run action to retry (since 2026-10-19 the New runs
  dialog with one week; see design section "Operator console redesign"). Connectivity failure is presented
  separately from analysis failure and polling retries automatically. No progress percentage is invented.
- `POST /runs` retains synchronous execution, returning a JSON summary and three CSV links.
  Standalone HTML responses and the `scorecard` field are removed, including for `Accept: text/html`.
  Amended 2026-10-17: the `out_dir` field and `X-Alerts-BI-Out-Dir` header are removed.
  Both operator API paths share the durable manual-execution gate. Busy submissions return 409;
  validation retains existing 400/422 behavior. Authentication and CSRF failures remain 401/403.

## Execution, recovery and compatibility

Admin owns migration 010_run_jobs and `alerts-bi-admin run-worker`. The UI only submits and
reads jobs. Run one dedicated worker process using the same admin image, registry mount and
SQL/ES/model settings as the UI (no writable output since 2026-10-17). No broker is required. Immutable migration 009_run_deletion is preserved; job state is additive revision 010.
Migration 011_run_job_keys adds a binary SHA-256 identity for the exact ASCII submission key,
backfills existing keys and replaces text-based uniqueness. It preserves case and trailing
spaces independently of database collation; raw keys remain in the private job audit.
The worker polls SQL and claims at most one submission. States are pending, running, completed
and failed. Completion never publishes a review. Portal views and shared 0.1.3 are unchanged. Operations 0.2.0 removes the public HTML renderer.

A SQL session application lock (`alerts-bi-manual-run`) serializes admission, execution and
recovery. The execution connection owns that lock across LLM-journal and persistence commits.
An active-slot constraint also prevents more than one pending/running job. The idle worker
checks that slot without taking execution locks, then rechecks/claims under the locks.
Submission deliberately refuses active manual/CLI/report/weekly operations with 409; it
does not create a queue behind them. Removing admission locks would violate this contract. The process-local
gate remains a compatibility guard. The existing alerts-bi-weekly SQL session lock on its
own connection protects the entire worker lifecycle from manual CLI runs, standalone reports,
weekly scheduling and permanent deletion of the job's own run. Admission also refuses a busy scheduler lock.
The durable pending/running slot reserves execution even after a worker's SQL sessions
close: CLI runs, weekly runs, reports and acceptance verification check the slot while
holding the shared lock and refuse until the worker completes or reconciles it. The
verification command holds that lock through all persistence and CSV writes. Job
admission and worker recovery bypass the slot check to recheck/claim/reconcile under
their existing locks; deletion retains its own transactional refusal and browser message.
All run-writing entry points must use the updated implementation before enabling jobs.

Run time/window, registry hash and deterministic run identity are captured on acceptance.
The worker refuses changed registry/model/version inputs before Elasticsearch queries rather
than silently analyzing different inputs. The accepted Monday 00:00 UTC boundary, 168-hour window and analysis logic are
unchanged. The idempotency key is scoped to the operator; retries return the same job, and
reusing a key with different options returns 409. A new exact key explicitly requests another attempt, including keys differing only in case
or trailing spaces. Lookup and uniqueness both use the binary key digest.

After the previous SQL session releases its lock, a replacement worker reconciles running
jobs. Other run writers remain refused while that durable reservation exists, so they
cannot replace the deterministic run ID between a worker's exit and reconciliation.
A completed run counts only when its started_at belongs to this attempt, not an older
run with the same deterministic ID. **Amended 2026-10-17:** a committed result becomes
completed with no warning; nothing is regenerated, because its CSVs render from SQL on
request. An uncommitted job becomes failed with an interruption message. No automatic
re-execution of interrupted analysis occurs. Pending jobs survive restart and execute
normally. Infrastructure failures exit the worker for its supervisor to restart. A failure
after SQL persistence still reports the committed analysis as completed.

Permanent deletion refuses the run's own pending/running manual job and removes related terminal
submission records in the same SQL transaction as the run. Since 2026-10-20 a job for another
run no longer blocks it: deletion goes ahead beside the worker, locking the active job's row
(design section 7.12). Their status URLs then return 404. Idempotency
records live until explicit deletion of their parent run.

## Outputs and rollout

**Amended 2026-10-17:** manual HTTP, worker and weekly runs write no files. Only explicit
exports write daily_metrics.csv, rule_counts.csv and alert_worklist.csv: `report` (`--out`,
default `out/<run id prefix>`), `run --out`, `weekly --out` and acceptance verification
`--out`. They remove the exact legacy scorecard.html in their selected output
directory. Standalone HTML routes, rendering and download links are removed entirely. The
normal authenticated team workspace remains the way to view results. Operations 0.2.0
builds/writes only the three CSVs and removes `report.html`; earlier immutable versions are
not rewritten. Consumers of that removed module must migrate to the workspace or CSV exports.

The `remove-run-html` command was removed on 2026-10-17: generated HTML existed only on the
emptyDir output volume, which every restart wipes.

Deploy the updated admin manifest with the same image digest in migration initialization,
UI and worker. Recreate terminates the old pod, then the new pod's migration init container
automatically applies missing revisions through 011 before any app container starts.
Failure blocks startup until initialization succeeds; no separate migration launch is needed. The
shipped admin manifest includes the dedicated executor with matching image, registry and
SQL/ES/model settings (no output mount since 2026-10-17). Keep both writers on the updated version: older
writers do not supply the required key digest introduced by 011. Verify rapid acceptance, completion/failure, duplicate submission, restart
recovery, authentication, publication separation and exports. No cluster deployment is authorized
by this source change. Preserve the additive job audit table and exact key identities during rollback; use a
compatible writer or forward repair. Do not downgrade or edit applied revisions 001–011.


## Operations, monitoring and interrupted rollout

Run one UI/worker pod with the Recreate strategy; the shipped YAML and canonical example
match. Docker Compose remains the existing local mock dependency stack: developers start
the separate worker command from the README. It does not deploy either application.
Submission, start and terminal transitions emit run.job_submitted, run.job_started and
run.job_finished logs with job/run identity; raw keys and model payloads are excluded.
Whole-run deletion intentionally removes its terminal job records, as part of the approved
permanent deletion contract, and remains audited through the existing admin.deleted event.
Since 2026-10-26 a failed submission whose run never stored results is deleted the same way
from its own page, which the team page's submissions list links to (design section 7.12).

Use [the worker incident runbook](manual-worker-runbook.md) for prolonged running age,
lost worker processes and rollback. Per-request SQL/ES/model timeouts already exist; there
is no global job deadline or automatic age-based cancellation. Production monitoring must
choose an age threshold and invoke the runbook. An old job must never be marked failed
while its SQL lock-owning worker is still running. This is operational guidance; no monitor,
cluster restart or production SQL action has been performed by this source change.

The CI integration job starts the pinned SQL Server/ES mocks, loads the fixed-clock fixture,
and executes the actual job, migration and critical acceptance tests. Preflight and a
zero-skip guard turn missing services or hidden fixture failures into CI failures.
