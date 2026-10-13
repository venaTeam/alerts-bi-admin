# Unified operator deployment

Use one admin image for the authenticated UI, migration jobs and weekly jobs, plus the separate
read-only portal image. Admin contains maintained engine source; no runtime wheel or separate
trigger image is needed. Preserve the existing login proxy and loopback-only application port
8200, identity header, CSRF tokens, SQL/ES/LLM settings, registry mount and writable output.

Follow [the OpenShift examples](openshift-deployment.md) and [ordered cutover/rollback](runs-retirement.md).
Stop the UI and manual worker, apply through migration 011, then deploy both from the updated
image. Run one dedicated manual worker using
`alerts-bi-admin run-worker` with matching registry, SQL/ES/model and ADMIN_OUT_DIR settings.
See [async runs](async-runs.md). Run one UI worker/replica and exactly one weekly scheduler. This is deployment preparation,
not a claim that infrastructure has been changed or verified on a cluster.
