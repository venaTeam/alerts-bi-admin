# Contracts

`teams.schema.json` is the registry format; values are maintained by runs. `outputs.json`
pins filenames and CSV columns; `docs/outputs.md` defines their meanings. `trigger-openapi.json`
is generated from the extraction baseline and checked against the runs application.
`migrations.json` pins the immutable SQL/revision bytes and schema head.
`sql-views.json` records column types/nullability from that migrated SQL Server schema.

Portal reads only published rows through the six `portal_*` views. Publications reject
overlapping windows, lock persisted runs against replacement, and support withdrawal.
Decisions are append-only under the authenticated actor, scoped by schema/application/key;
they never rewrite a quality state or transfer to a changed key. See design sections
7.10–7.14 and the preserved integration tests for transactional and disclosure invariants.

Run `python scripts/check_contracts.py` in an integration environment with all application
wheels installed. `--sql` additionally compares the real disposable database metadata.
Expected contracts are not regenerated as a way to make a failing check pass.

The 2026-10-08 console retains trigger payloads and output filenames behind operator authentication. `trigger-openapi.json` remains the compatibility payload baseline; deployment security and UI routes are defined by `console-http.json`. The legacy factory is not a deployment entry point.

Current producer: admin owns registry artifacts, execution adapters and the unchanged migration tree; portal consumes the shared library and published SQL views. Runs references in recorded source revisions are historical provenance.

The 2026-10-13 async amendment adds migration 010 and console run-jobs routes. The subsequent
HTML-removal request changes synchronous POST /runs to JSON only, removes its scorecard field
and all standalone HTML routes, and restricts outputs.json to three CSV files. Operations
0.2.0 removes report.html and renders only CSV; older immutable versions are preserved.
See docs/async-runs.md.
The approved 2026-10-13 lifecycle adds Monday-only manual run boundaries and the admin
delete POST, removes the admin gap override, and appends migration 009 without modifying
001–008. Decisions remain append-only while their parent run exists; permanent run deletion
is the explicit whole-run exception. Portal views and shared interfaces
are unchanged; the later HTML-removal amendment changes operations to 0.2.0. `trigger-openapi.json` now records the approved Monday/default description.

Review fixes add 011_run_job_keys for collation-independent exact submission identity.
Migration 009 remains deletion; 010 remains jobs. Recovery regenerates all three CSVs
from committed SQL before reporting clean completion. See docs/async-runs.md for rollout.

console-openapi.json is generated from the actual authenticated console and includes the
run-jobs request body, required Idempotency-Key/CSRF headers and typed status responses.
trigger-openapi.json remains the legacy synchronous factory baseline. The live console still
disables public OpenAPI/docs routes; the committed artifact supports API consumers and checks.

The 2026-10-14 backfill amendment (docs/backfill.md) appends migration 014_backfills without
modifying 001–013: the run_backfills table, run_jobs.backfill_id, and the queued and cancelled
job states. console-http.json records the backfill routes, claim order and weekly wait, and
console-openapi.json is regenerated with BackfillOut and the extended job states. Portal views,
CSV outputs and shared/operations interfaces are unchanged.
