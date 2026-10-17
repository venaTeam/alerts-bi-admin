# Multi-week backfill — accepted 2026-10-14

The product owner reviewed an interactive mock of the admin console on 2026-10-14 and approved
an operator backfill: an operator picks completed Monday weeks for one team, and the console
queues one run per week that the worker executes one at a time, oldest first. This amendment
supersedes the post-MVP backfill described in design sections 3.5, 5.1 and 6 (an automatic,
deterministic-only, model-free sweep), the excluded-scope entry for model classification of
backfilled history, the matching flow and blueprint steps, and the async contract's refusal to
queue work while a backfill is in progress. The weekly scheduler still never backfills on its own.

## Product decisions (user, 2026-10-14)

1. **The model is on.** Backfill offers the same live/fake/off model mode as New run, with live
   as the default. Each week is assessed exactly like a weekly or manual run of that window:
   exhaustive coverage, stored verdicts reused by identity, unchanged prompt and model versions,
   audit and retry behavior. `llm_assessed` keeps its run-level meaning; a backfilled run is
   not implicitly unassessed. Model usage grows with the number of weeks.
2. **One run at a time, oldest first.** A New run or the weekly schedule can go ahead of the next
   queued week.
3. **Only published weeks are locked.** A week with a current publication cannot be picked;
   withdraw it to run it again. A week with an unpublished run can be picked and is run again;
   as for any manual rerun, persistence replaces the earlier run only when its deterministic
   identity (registry and model versions) is unchanged, otherwise both runs remain.
4. **Cancel removes only queued weeks.** The running week finishes, completed weeks stay, and a
   failed week can be retried on its own.
5. **One team per backfill**, started from that team's workspace.
6. **Publishing stays manual.** Backfilled weeks complete unpublished and are published one at a
   time with the existing publication rules.
7. **The picked Monday is the window end**, 00:00 UTC, the same meaning as New run. Only completed
   weeks whose window start is inside the 84-day Elasticsearch retention can be picked.
8. **The calendar week starts on Sunday.** Mondays remain the only selectable days.

## Selection and admission

The team workspace has a Backfill weeks action beside New run. Its dialog shows month calendars
covering the retained range with Sunday as the first column. Each Monday shows its state: no
completed run, unpublished run, published (locked), already queued or running (locked), past
retention, or not complete yet. The dialog lists the selected weeks in run order with their exact
UTC windows, warns when a selectable week leaves retention within seven days, and offers the
live/fake/off model mode with live selected.

`POST /teams/{team_id}/backfill` uses the console's same-site actor-bound CSRF form and a
per-form submission key. Repeating the key returns the same backfill; reusing it with different
weeks or model mode is refused with 409. The server accepts 1–12 distinct Mondays and refuses
the whole request (400 or 409, nothing stored) when the team is not registered, a week is not a
Monday 00:00 UTC boundary, is not yet complete, starts more than 84 days before acceptance, has
a current publication for the team, or already has a queued, pending or running job for the team,
or when the team already has an active backfill.

Admission stores one `run_backfills` row and one queued `run_jobs` row per week in one SQL
transaction under the team's transaction-scoped queue lock (`alerts-bi-queue:{team_id}`). Every
run job insert and run deletion take the same lock, so a deletion cannot remove a week or run
queued after its own checks. Admission performs no Elasticsearch or model call and does not
take the execution lock. As for manual jobs, each week's registry hash and deterministic run
ID are captured at acceptance.

## Queue and execution

Run jobs gain two states. `queued` reserves nothing: other writers (CLI runs, reports, weekly
scheduling, deletion) still check only the active pending/running slot. `cancelled` is terminal.

The single worker claims work under the existing execution locks in this order: reconcile
orphaned running jobs, then a pending job, then queued manual jobs by submission time, then
backfill weeks by backfill submission time and oldest week first. A claimed week becomes running
and takes the active slot. Execution, the refusal of changed registry/model inputs, interruption
recovery and completion are unchanged (since 2026-10-17 recovery regenerates no CSV files);
completion never publishes.

Manual submissions are unchanged when no backfill is in progress, including the 409 while busy.
While a backfill has queued, pending or running weeks, a manual submission that finds the slot or
execution lock busy is accepted as `queued` and runs before the next backfill week. The JSON
status reports `queued` until the worker claims it.

When a weekly invocation finds the shared lock or slot busy while a backfill is in progress, it
holds the SQL session lock `alerts-bi-backfill-yield` and polls every 30 seconds for up to three
hours. While that lock is held the worker claims no further backfill weeks, so the weekly run
starts when the current week finishes. If the wait expires, the invocation refuses as before and
the next daily invocation retries. Without an active backfill the weekly refusal is immediate.

Cancelling a backfill marks its queued weeks cancelled and records the operator and time. A
single queued week can also be removed. A failed backfill week can be retried: a new queued job
joins the same backfill with a freshly captured registry hash and run ID. Retry is refused when
the week is now published or active, or when another backfill for the team is active. A finished
backfill can be dismissed from the workspace; retrying it later restores it. Run deletion is refused while a queued job targets
that run; the existing active-slot refusal and job-record removal are unchanged.

## Display

An active backfill always appears above the team's runs, even one dismissed before a retry or
older than a finished backfill; otherwise the newest finished backfill that is not dismissed does:
completed/running/queued/failed/cancelled counts, a progress bar of completed weeks, each week's
status and actions (open the run, remove a queued week, retry a failed week), and cancel, retry
and dismiss actions. While weeks are active the page polls `GET /backfills/{backfill_id}` every
three seconds, pauses while hidden, and reloads when a week changes state. No duration estimate
is shown. Runs created by a backfill show the source "Backfill". A banner shows how many retained
weeks have no completed run and when the oldest leaves retention.

## HTTP routes

| Route | Purpose |
|---|---|
| `POST /teams/{team_id}/backfill` | Accept a backfill (browser form) |
| `GET /backfills/{backfill_id}` | JSON backfill state for polling |
| `POST /backfills/{backfill_id}/cancel` | Cancel all queued weeks |
| `POST /backfills/{backfill_id}/retry` | Retry every failed week |
| `POST /backfills/{backfill_id}/dismiss` | Hide a finished backfill |
| `POST /run-jobs/{job_id}/cancel` | Remove one queued week |
| `POST /run-jobs/{job_id}/retry` | Retry one failed backfill week |

All require operator identity; POSTs require the same-site actor-bound form token. The run job
status values become pending, queued, running, completed, failed and cancelled.
`contracts/console-openapi.json` is regenerated from the console.

## Schema and rollout

Migration `014_backfills` adds `run_backfills`, a nullable `run_jobs.backfill_id` foreign key,
a queue index, and replaces the run job state check so `queued` and `cancelled` rows hold no
slot. Applied revisions 001–013 are unchanged. Deployment applies 014 through the existing
migration init container before the UI and worker start; UI and worker must run the same image.
Before rolling back to an image without migration 014, stop new submissions and leave no queued
job of any kind: a New run accepted during a backfill is queued outside that backfill, so
cancelling backfills does not remove it. Cancel or finish active backfills, let the current
worker run the remaining queued manual jobs, or terminalize them with the worker runbook's
procedure. An older worker ignores queued rows and leaves them queued indefinitely. The additive
schema can remain installed.

Portal views, CSV outputs and columns, scoring, prompt text, deterministic identifiers, the
publication rules and weekly planning are unchanged. No cluster deployment is authorized by this
amendment.
