# Unified operator deployment

Use one admin image for the authenticated UI, migration jobs and weekly jobs, plus the separate
read-only portal image. Admin contains maintained engine source; no runtime wheel or separate
trigger image is needed. Preserve the existing login proxy and loopback-only application port
8200, identity header, CSRF tokens, SQL/ES/LLM settings, registry mount and writable output.

Follow [the OpenShift examples](openshift-deployment.md) and [ordered cutover/rollback](runs-retirement.md).
Apply the updated admin Deployment with the same image digest in its migration init container,
UI and manual worker. Recreate stops the previous pod; initialization automatically applies
missing migrations through 011 before the UI/worker start. Failure blocks startup. The SQL
login must allow schema migrations. No separate migration job is needed for normal rollout.
Run one dedicated manual worker using
`alerts-bi-admin run-worker` with matching registry and SQL/ES/model settings (ADMIN_OUT_DIR was
removed on 2026-10-17; runs write no files).
See [async runs](async-runs.md). Run one UI worker/replica and exactly one weekly scheduler. This is deployment preparation,
not a claim that infrastructure has been changed or verified on a cluster.
