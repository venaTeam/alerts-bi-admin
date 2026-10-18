# Alerts BI unified operator and analysis application

This repository owns the authenticated operator UI/API, analysis engine, SQL migrations,
weekly jobs, team registry, mock tooling, and shared/operations library sources. It replaces
the former runs service and source repository. The standalone portal remains a read-only
consumer of published SQL views and `alerts-bi-shared`.

Teams opens run history and Overview, Findings, Decisions and Activity tabs. Schedule and
Decisions have workspace-wide pages. Trigger, inspect, publish, withdraw, delete runs and record human
decisions in the same application. The existing UI is preserved.

## Install and start

Python 3.12+ and Poetry 2.3.1 are required. A standalone clone includes all source and resources:

```powershell
poetry sync --no-root
poetry run python -m ensurepip --upgrade
poetry run python -m pip install --no-deps -e .
Copy-Item .env.example .env
# Configure SQL_*, ES_*, LLM_* and a private ADMIN_SECRET of at least 32 characters.
poetry run alerts-bi-admin db setup
poetry run alerts-bi-admin serve
```

`db setup` targets SQL_DATABASE unless `--database` is explicit; run it only for an authorized
database. It preserves applied revisions and rejects checksum drift. No data/schema migration
is introduced by the repository move. The run-deletion feature requires schema head
`009_run_deletion`; apply it through `db setup` before using Delete run. It preserves
append-only decision protection while a run exists. Never edit applied migration files.

The listener binds only to loopback, default port 8200, behind the trusted login proxy's
`X-Forwarded-User`. Local-only `serve --dev-user your-name` supplies an explicit development
identity. Every HTTP route requires identity. Writes require actor-bound CSRF tokens and
same-site requests; JSON `POST /runs` gets its token from authenticated `GET /csrf`.
`GET /healthz` checks SQL, Elasticsearch and operator schema, returning a redacted 503 when
unavailable. Keep one UI worker/replica; manual analysis is synchronous and uses a process gate.

## Commands

```powershell
poetry run alerts-bi-admin run --team checkout-api --fake-llm --database alerts_bi_test
poetry run alerts-bi-admin weekly --dry-run
poetry run alerts-bi-admin weekly
poetry run alerts-bi-admin weekly-status
poetry run alerts-bi-admin db status
poetry run alerts-bi-admin registry check --registry config/teams.json
poetry run alerts-bi-admin report --run-id <id>
poetry run alerts-bi-admin publications --team <team>
poetry run alerts-bi-admin publish --run-id <id> --by <operator>
poetry run alerts-bi-admin unpublish --run-id <id> --reason <reason> --by <operator>
poetry run alerts-bi-admin decide --run-id <id> --schema v1 --application <app> --key-field <key> --finding R1 --state confirmed --note <note> --by <operator>
poetry run alerts-bi-admin decisions --team <team>
```

Manual runs select a Monday 00:00 UTC end and cover the preceding Monday-to-Monday
168 hours, remaining unpublished. The form uses a Monday-only date picker; JSON and CLI
reject other weekdays or times. Omitted `run_at` / `--run-at` uses the latest completed
Monday week. Historical runs and the fixed-clock acceptance oracle remain readable.
Weekly jobs retain
the Monday UTC windows, SQL schedule lock, health gate, catch-up and publication policy.
Run exactly one weekly scheduler. Operator `--by` defaults to the OS user; web writes use the
proxy identity. Operator commands support `--database`, while the listener additionally
supports `ADMIN_DATABASE`. Registry is admin-owned `config/teams.json`; mount production values
using `ADMIN_REGISTRY_PATH` or an explicit `--registry`. The listener, registry check,
manual runs, weekly jobs and weekly status all honor that environment variable.
Runs and weekly jobs write no files: the three CSV downloads render from SQL on request.
`report` exports a stored run's CSVs to `--out` (default `out/<run id prefix>`), and
`run --out` and `weekly --out` also export. Deploy the updated manifests (no ADMIN_OUT_DIR,
output mounts or weekly `--out`) with this image; roll back with the previous manifests and image. An explicit `--registry` takes precedence over
`ADMIN_REGISTRY_PATH`.

Delete run is an authenticated, CSRF-protected POST with explicit permanent-deletion
confirmation. It removes the run's SQL results, publications, decisions, schedule rows and
model audit transactionally. It retains shared caches and prompt artifacts. Runs write no
report files. CLI exports are not tracked: CSVs written by `report` (default
`out/<run id prefix>`) or any `--out`, including `alert_worklist.csv`, stay on that disk after
deletion until removed by hand. Active
manual/model/weekly work blocks deletion. Manual HTTP and CLI runs take the same SQL
execution lock as scheduling and deletion, covering scanning, persistence and report
writing even with assessment off. Roll out the updated manual CLI alongside the app;
older CLI versions do not take this lock. The scheduler can recreate a deleted due week.
The admin publication dialog has no gap override; missing weeks still block publication.
CLI recovery and scheduler retention exceptions retain their existing gap handling.

### Elasticsearch sources

For an existing deployment, see the [environment changes and migration guide](docs/admin-env-changes.md).

Set `ES_V1_INDEX` and `ES_V2_INDEX` to the deployment's v1 and v2 index names, aliases,
or read patterns. They default to `appchi-v1` and `appchi-v2` for the existing local mock.
The reader and `/healthz` use the same settings; readiness requires both configured
sources to exist. Whitespace in an index setting is rejected before querying.
Settings load from `.env`, with the process environment taking precedence.
`ES_PIT_KEEP_ALIVE` controls the point-in-time lifetime for both opening and paging
the snapshot (default `2m`); use a positive duration such as `5m` or `120s`.

The mock generator, scale probe and Kibana data-view lookup use these index settings
too. The generator requires distinct concrete names and still refuses `RESET=1` on
non-loopback endpoints. Use an isolated local mock for generation/reset; source aliases
and wildcard patterns are for readers. The scale probe also honors `ADMIN_REGISTRY_PATH`.
Pure fixture helpers, fake evaluation, and CPU/memory benchmarks accept wildcard settings;
concrete-name validation happens before mock reset or bulk writes. Readiness rejects
wildcard sources with no matching indices.

## Source, libraries and builds

UI source is directly in `src/`; engine code is in `src/engine`, installed under
`alerts_bi_admin.engine`. Historical revisions use local and installed `src.db.ledger`
compatibility bridges. Never edit applied SQL/revision files. Public library namespaces remain
`alerts_bi_shared` and `alerts_bi_operations`, maintained in `packages/` as local Poetry path dependencies.
There is no runs runtime package or sibling checkout dependency.

`poetry sync --no-root` installs the locked dependencies and editable local libraries.
Existing uv-created `.venv` environments may lack pip: the `ensurepip` setup step
bootstraps it before installing the application. Keep the three setup commands together.
The application uses setuptools for its `src` to `alerts_bi_admin` mapping and
migration compatibility bridge; Poetry's root editable installer cannot preserve that
mapping. `poetry build` uses the declared setuptools backend. Docker builds and installs
the app and both library wheels for a non-editable production environment.
Use `poetry check --lock` to check the lockfile. Current service commands are in this
README; the pinned `docs/upstream/` snapshot retains its historical tooling examples.

```powershell
poetry build
poetry run python scripts/build_libraries.py
docker build -t alerts-bi-admin:local .
```

Portal pins an immutable shared wheel; copy each new versioned artifact to its vendor directory
and update its lockfile only after verifying compatibility. Never overwrite a released wheel.
An admin wheel install requires matching shared/operations wheels; the Docker build uses only
this checkout and committed lockfile. Source provenance is in `docs/engine-source-import.json`.

## Local mocks and verification

Manual browser runs now return immediately after saving a SQL-backed submission. Apply
`alerts-bi-admin db migrate` before starting the updated app, then run
`alerts-bi-admin run-worker` in a separate terminal/process with matching ADMIN_DATABASE,
ADMIN_REGISTRY_PATH, SQL, Elasticsearch and model settings. The worker does not
require ADMIN_SECRET or a writable output directory. The shipped deploy/admin.yaml includes a
dedicated worker with the same image, registry and SQL/ES/model configuration. Its migration init container
automatically runs db setup before UI/worker startup, targeting ADMIN_DATABASE or SQL_DATABASE.
Recreate stops the previous pod before initialization; migration failure blocks startup. The
health endpoint requires the job-key digest, stored read models and private checkpoint
tables through migration 013 and the backfill state from migration 014, so an older schema
returns 503 until upgraded. Migration 011 backfills
existing submission digests without editing 001–010. Older writers cannot insert the new
required digest. Keys differing only in case or trailing spaces request distinct attempts.
Pending/running jobs retain their execution reservation after worker exit. CLI/weekly
runs, reports and acceptance verification refuse until the worker completes or reconciles
the job; use the updated image for every run writer. The verification command also holds
the shared SQL lock through persistence and exports.
`run-worker --once` processes at most one job and exits. The team page polls while visible;
you can leave and return later. Failures and worker interruptions remain visible and need a
new submission to retry. Analysis completion never publishes a review.

JSON clients use authenticated `POST /run-jobs` with X-CSRF-Token and Idempotency-Key, then
GET the returned status_url. Existing synchronous POST /runs returns JSON only and shares
the manual SQL gate. Workers retain the shared weekly/manual CLI/report/deletion execution lock for their complete
lifecycle. Pending/running jobs prevent permanent deletion. See docs/upstream/async-runs.md for the full contract.

Backfill weeks in the team workspace queues one run per selected Monday week for that team,
oldest first, with the New run model mode (live by default). Only completed weeks inside the
84-day Elasticsearch retention can be picked, and published weeks are locked. Migration 014
adds the backfill table and the `queued`/`cancelled` job states; queued weeks hold no slot,
so other writers are only refused while a week actually runs. The same worker claims queued
manual runs before the next backfill week. A weekly invocation that finds a backfill week
running waits up to three hours, keeping the worker off further weeks until it has run.
Cancel removes only queued weeks; a failed week can be retried. See docs/upstream/backfill.md.

Explicit `report`, `run --out`, `weekly --out` and acceptance output directories contain
three CSV exports. Standalone HTML
scorecards are removed entirely, including on-demand routes and download links. The normal
team workspace remains available. POST /runs returns JSON only, without a scorecard field.
Since 2026-10-17 it also omits the `out_dir` field and the `X-Alerts-BI-Out-Dir` header, a
breaking change for callers that read them; use the `files` download links instead.
Operations 0.2.4 uses shared 0.1.7 with stored summaries and pooled web reads. Shared 0.1.7 applies `SQL_ENCRYPT=true` as required TLS through pymssql 2.4.3 or later; the server certificate is not validated, so TLS stops passive reading but not an active interceptor. It rejects unknown `SQL_ENCRYPT` values, refuses `SQL_TRUST_SERVER_CERTIFICATE=false` (the driver cannot validate certificates), and logs `sql.unencrypted` once per process for each SQL server when encryption is off. Web apps refuse an unenforceable setting at startup; commands and jobs refuse it before their first SQL connection. CSV columns remain unchanged; the standalone HTML removal introduced in operations 0.2.0 remains in effect.

The original mock generator, scripts and hand-reviewed oracle now live here. The Compose
project defaults to `alertsbi` to reuse the existing named volumes. For an existing installation
with another project name, set COMPOSE_PROJECT_NAME to its existing name before using Compose.
Never use `down -v` for a source relocation. Configure `.env` before starting local dependencies.

```powershell
docker compose up -d
$env:STATS_ONLY = "1"
poetry run python scripts/generate_mock_alerts.py
Remove-Item Env:STATS_ONLY
poetry run ruff check .
poetry run ruff format --check .
poetry run mypy
poetry run pytest tests/unit tests/engine/unit -q
poetry run pytest tests/integration tests/engine/integration -q -rs
poetry run pytest tests/engine/acceptance -q -rs
poetry run alerts-bi-admin verify-acceptance --database alerts_bi_test
```

Integration suites require real local SQL Server and the seeded Elasticsearch mock. They reset
only disposable `alerts_bi_test`; run them serially, with no other session testing that database.
Acceptance uses the original fixed-clock fixture load; never regenerate expected-results.json.
`scripts/evaluate_llm.py`, `benchmark_streaming.py`, `es_scale_probe.py` and `create_kibana_panels.py`
retain their existing explicit options. Live model evaluation is opt-in and separate from fake tests.

For a modest local rehearsal that retains six months of history, use:

```powershell
poetry run python scripts/benchmark_reads.py --seed --teams 3 --weeks 27 --heavy-teams 3 --heavy-identities 500 --heavy-events 10000 --concurrency 2 --requests 84 --read-committed-snapshot --output out/laptop-reads.json
```

This seeds 40,500 historical findings, then writes another 500 during measurement.
It verifies local behavior, not capacity at the full target. Run larger tests only on a
host with sufficient SQL Server memory and storage. The following full-scale command
is intentionally separate from the small local rehearsal.

`poetry run python scripts/benchmark_reads.py --seed --requests 280` measures the real admin
and independently installed portal HTTP entry points with 100 teams and 27 weekly runs.
It requires the portal package installed in that dedicated benchmark environment and resets
only local `alerts_bi_test`; never run it alongside integration tests. The default distribution
has one 20,000-identity team, nine 2,000-identity teams and ninety 50-identity teams. Increase
`--heavy-teams` and `--heavy-identities` explicitly for larger populations. Evidence includes
cold/warm and concurrent-ingestion timing, SQL rows, memory and storage in `out/scale-reads.json`.
Use `benchmark_streaming.py --events 1200000 --identities 20000 --mode streaming` for actual
event-processing cost. See `docs/upstream/read-path-performance.md` for contracts and targets.
Repeat reads with `--read-committed-snapshot` to measure committed versioned reads during
ingestion. This option changes only local `alerts_bi_test`; production enablement requires
the separately authorized, drained-connection procedure documented in the design.

Runs write no files, so the admin image needs no writable output directory; explicit CLI
exports write to their own `--out`. Deployment examples and
the ordered scheduler/migration transfer, rollback and eventual archival checklist are in
`docs/upstream/openshift-deployment.md` and `docs/upstream/runs-retirement.md`. PRs do not deploy
or stop services. Canonical requirements stay in alerts-bi-design; snapshots here are pinned.


The Checks workflow also runs real SQL/ES integration and critical acceptance tests on
disposable CI mocks using scripts/ci_integration.py; missing services and skipped checks fail
that job. Local Compose supplies dependencies only; start the manual worker separately.
For prolonged running jobs and rollback, follow docs/upstream/manual-worker-runbook.md.
The idle worker checks for work before taking application locks. State transitions are logged.
The committed console-openapi.json contract describes async submissions and status; the live
console keeps public schema endpoints disabled. Operators have shared access across teams.


For automatic deployment migrations, build the updated image and set the same digest in
the migrate init container, admin and run-worker entries of deploy/admin.yaml, then apply
that manifest. The configured SQL credentials must allow schema migrations. No separate
deploy/migrate.yaml Job is needed for normal rollout; it remains an optional maintenance
tool with UI/worker writers stopped. Do not run it alongside deployment initialization.
