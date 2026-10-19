# Unified operator deployment

Use one admin image for the UI, migration init container, manual worker, weekly
CronJob and one-off admin jobs, plus the separate read-only portal image. Admin contains
maintained engine source; no runtime wheel or separate trigger image is needed. Since
2026-10-19 the console has no login proxy or identity (design "Admin console without login"):
it listens on port 8200 behind an edge-TLS Route, and keeps its CSRF tokens, SQL/ES/LLM
settings and registry mount. Since 2026-10-17 runs write no files, so no writable
output is needed.

Deploy with the runbooks in the application repositories (amended 2026-10-19): admin's
`deploy/openshift/README.md`, then portal's `deploy/openshift/README.md`. They implement
[the OpenShift requirements](openshift-deployment.md) and the
[ordered cutover/rollback](runs-retirement.md). The admin Deployment's migration init container,
UI and manual worker share one image digest. Recreate stops the previous pod; initialization
automatically applies every missing migration before the UI/worker start, and failure blocks
startup. The SQL login must allow schema migrations. No separate migration job is needed for
normal rollout. The dedicated manual worker runs `alerts-bi-admin run-worker` with the same
registry and SQL/ES/model settings (ADMIN_OUT_DIR was removed on 2026-10-17; runs write no
files). See [async runs](async-runs.md). Run one UI worker/replica and exactly one weekly
scheduler. This is deployment preparation, not a claim that infrastructure has been changed
or verified on a cluster.
