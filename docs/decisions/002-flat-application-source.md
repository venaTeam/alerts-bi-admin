# 002 — Application code directly in src

Accepted 2026-10-06 through the product owner's request to remove the extra service directory.

Each app's modules and subpackages live directly in `src/`. Local tests and scripts import
`src`, and application-to-application imports remain forbidden. Application code uses relative
imports so the same files work in local source checks and their installed package namespace.

Setuptools maps `src/` to the unique `alerts_bi_runs`, `alerts_bi_portal` or
`alerts_bi_admin` package in wheels and editable installs. This preserves entry points and
allows the design integration environment to install all three apps together. The apps keep
separate local environments. Shared and operations retain their existing package layouts,
versions and consumer wheels.

Runs moves its historical `src.db.ledger` bridge to `compat/src/`; the wheel includes it.
Applied revisions and SQL files stay byte-identical. The Alembic script location becomes
`src/db/migrations`. Guides, prompt hashes, registry/SQL/API/output contracts, `APP_VERSION`
and runtime behavior remain unchanged. No schema rollout or data movement is needed.

Validate each app's local checks, installed wheel outside its checkout, frozen Docker build,
coordinated contract/integration checks and existing acceptance oracle. Recovery is restoring
the prior compatible app commits/images; no database rollback is required.
