# Runs retirement and cutover — 2026-10-12

Status: approved implementation scope; PRs and local checks are not deployment evidence.

## Ownership and acceptance

| Repository | Maintained responsibility |
|---|---|
| admin | Unified authenticated UI/API, engine, migrations, weekly execution, registry, mock tooling and library builds |
| portal | Separate GET/HEAD-only reader of published SQL views; shared library only |
| design | Requirements, contracts, source mapping, pinned snapshots and coordinated verification |
| runs | Historical source and retirement notice; no future releases after cutover |

The integrator owns all four isolated task worktrees. Import engine source from runs
`89f6c88aff7a7c225f4e2a20485ebd8660db3ddb`; preserve the admin UI at `a9ddb26` and portal at
`a00bc03`. Import the exact library source already delivered in 0.1.2 after comparing it
with the producer's preserved local changes. Record source hashes in admin's
`docs/engine-source-import.json`. Do not copy the abandoned runs-hosted console.

Sequence: establish this ownership decision; import source/resources and unify commands;
build shared/operations 0.1.3 and update portal; verify isolated packages and the real local
SQL/ES workflow; pin canonical snapshots; deliver coordinated PRs and tested revisions.

Acceptance requires a standalone admin install without any runs package or checkout;
unchanged applied migration, guide/prompt and oracle bytes; all required app/library checks;
fresh/idempotent migrations and weekly behavior on disposable SQL Server; authenticated manual
trigger, publish/decision/withdrawal and exports; portal compatibility and rendered UI inspection.
Serialize suites that reset `alerts_bi_test`. Do not reset development or production data.

## Replacement commands and artifacts

| Previous entry point | Replacement |
|---|---|
| runs web listener | `alerts-bi-admin serve` behind the existing login proxy, port 8200 |
| `alerts-bi-runs run` | `alerts-bi-admin run` (same flags) |
| `alerts-bi-runs db setup/migrate/status` | `alerts-bi-admin db setup/migrate/status` |
| `alerts-bi-runs weekly` / `weekly-status` | `alerts-bi-admin weekly` / `weekly-status` |
| `alerts-bi-runs report` / `registry check` / `verify-acceptance` | Same subcommands on `alerts-bi-admin` |
| runs image for migration/weekly jobs | The same verified admin image digest used for the UI |
| library source/builds in runs | `admin/packages/*`; `python scripts/build_libraries.py` |
| mock stack, generator and acceptance oracle | `admin/docker-compose.yml`, `scripts/`, `test/fixtures/` |

The older `alerts-bi` executable is not installed by admin. Update automation explicitly.
HTTP payloads and exports remain compatible, with identity and CSRF requirements already
introduced by the unified console. Configure `SQL_*`, `ES_*`, `LLM_*`, `ADMIN_*`, mounted
registry and writable output. Keep one UI worker/replica and one weekly scheduler.

## Authorized deployment procedure (execute separately)

1. Review and merge the design, admin, portal and retirement PRs in dependency order. Record
   the merged commits and build immutable admin/portal images plus library artifact hashes.
2. Inventory old trigger consumers, CronJobs, migration jobs, registries, image references,
   secrets, routes and monitoring. Preserve the current registry bytes and database backup.
3. Suspend the old weekly CronJob and wait for its active Job and in-flight manual analyses
   to finish. Do not enable a replacement scheduler while an old one is active.
4. Run `alerts-bi-admin db status --database <existing-database>` from the new image, then
   `db setup` once. Expect schema head 008 and no new applied migrations. Do not reset, stamp,
   downgrade, copy or recreate the database. Stop rollout if ledger checks differ.
5. Replace the existing authenticated admin application container with the new image. Keep
   the proxy, service account, authorization, TLS and loopback upstream. Supply the engine's
   ES/LLM settings, registry mount and writable output. No `--dev-user` in deployment.
6. Verify unauthenticated denial, authenticated health, team/run navigation, exports and
   an approved test run plus publication/decision/withdrawal flow. Validate the portal using
   the same SQL database and compatible shared artifact.
7. Switch the existing weekly CronJob to the same admin digest and `alerts-bi-admin weekly`.
   Keep the registry, SQL settings, UTC schedule, concurrency policy and SQL lock. Inspect
   `weekly --dry-run` and `weekly-status`; enable exactly one scheduler and verify its next Job.
8. After migrating authenticated API callers and verifying monitoring, remove the obsolete
   trigger Deployment/Service/Route and old migration-job image references. Preserve rollback
   images, records, secrets still needed by admin, and all persistent data.

## Rollback and eventual archival

Rollback restores the previously verified authenticated admin image and job definitions.
Suspend/drain the new scheduler before re-enabling the old scheduler. Preserve SQL records,
publication/decision history and migration ledgers; never downgrade or reset data. Do not
restore an unauthenticated public trigger route.

Archive runs only after all PRs are merged, replacement deployments and at least one scheduled
cycle have been verified, all consumers/build pipelines/docs point to admin, outstanding work
is transferred, and the team's agreed rollback retention period has elapsed. Confirm no active
job or release still uses its image/package. Obtain explicit archival authorization, archive
the repository read-only, and retain Git history and release artifacts. Do not delete it.
