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

The 2026-10-13 async amendment adds migration 009 and console run-jobs routes, preserving the
synchronous trigger baseline. outputs.json distinguishes stored CSV files from on-demand HTML;
the operations library renderer retains its public four-file interface. See docs/async-runs.md.
The approved 2026-10-13 lifecycle adds Monday-only manual run boundaries and the admin
delete POST, removes the admin gap override, and appends migration 009 without modifying
001–008. Decisions remain append-only while their parent run exists; permanent run deletion
is the explicit whole-run exception. Portal views and shared/operations library interfaces
are unchanged. `trigger-openapi.json` now records the approved Monday/default description.
