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
