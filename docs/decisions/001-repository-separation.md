# 001 — Four repositories, shared SQL, versioned libraries

Accepted 2026-10-06 through the product owner's implementation request.

Use design, runs, portal and admin repositories in `venaTeam`. Preserve all approved
product behavior. Runs retains source history; new repos record the extraction source
in `PROVENANCE.json`. The original checkout remains available for recovery.

Runs owns the single migration chain and two separately installable libraries. Portal
depends only on shared; admin depends on shared and operations. To make independent
clones build immediately without inventing a private package service, consumers commit
the small immutable library wheels in `vendor/`; `uv.lock` pins their SHA-256. Rebuild a
changed library with a new distribution version and update consumers deliberately.
Library release versions are independent of the analysis `APP_VERSION` used in run IDs.

Design owns schema, SQL/API/output contracts and integration checks. Applications carry
verified `docs/upstream` snapshots and a source revision/hash manifest. These are pinned
inputs, not separately editable authorities. Contract changes start in design, then runs
implements compatible expansion, consumers update, and removals wait for compatibility.

Shared SQL access remains an application boundary rather than a new least-privilege
database guarantee. Registry values are maintained only in runs and supplied to admin.
No deployment, data migration or reset of development/production data is part of cutover.
