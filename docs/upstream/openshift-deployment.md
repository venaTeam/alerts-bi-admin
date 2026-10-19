# OpenShift deployment: unified admin and read-only portal

Current topology, approved 2026-10-12; amended 2026-10-17 to remove run output files and their
mounts; amended 2026-10-19 to move the executable procedure into the application repositories.
Nothing here has been executed on a cluster. Admin is the only operator/analysis image. The
separate portal remains read-only. See [retirement cutover and rollback](runs-retirement.md)
before changing a runs-era deployment.

## Where the procedure lives

Amended 2026-10-19. The deployment procedure and its files live with the code they deploy, so a
deploying agent works from the release checkouts and supplies only the listed values:

| Repository | Files | What they do |
|---|---|---|
| admin | `deploy/openshift/README.md` | The runbook: values, fresh install, upgrade from the runs era, migration gate, verification, weekly scheduler, day-2 changes, rollback, troubleshooting |
| admin | `deploy/openshift/admin.yaml` | OpenShift Template: ServiceAccount, Deployment (migrate init container, admin, run-worker), Service, Route, CronJob `alerts-bi-weekly` |
| admin | `deploy/openshift/admin-job.yaml` | OpenShift Template: one `alerts-bi-admin` command as a Job (`db status`, `registry check`, the maintenance `db setup`) |
| admin | `deploy/openshift/{values,config,secrets}.env.example` | Template values, the `alerts-bi-config` ConfigMap keys and the `alerts-bi-secrets` keys |
| portal | `deploy/openshift/README.md`, `portal.yaml`, `values.env.example` | The portal runbook and Template: ServiceAccount, Deployment, Service, Route |

This document keeps the requirements those files implement. The templates are rendered with
`oc process --local --param-file`; configuration and credentials are created from env files with
`oc create --from-env-file`, so no secret enters a template or repository. Unit tests in each
repository keep templates, value files and the settings the code reads in step. The earlier
manifests (`deploy/admin.yaml`, `deploy/migrate.yaml`, `deploy/weekly.yaml`) and the YAML copies
that were in this document are replaced. Deploy admin first, then the portal.

## Cluster objects and configuration

| Object | Kind | Created by | Read by |
|---|---|---|---|
| `alerts-bi-admin` | Deployment, Service, Route, ServiceAccount | `admin.yaml` | anyone with the Route URL (no login) |
| `alerts-bi-weekly` | CronJob, created suspended | `admin.yaml` | — |
| `alerts-bi-config` | ConfigMap of non-secret settings | operator, from `config.env` | admin, its jobs, portal |
| `alerts-bi-secrets` | Secret: SQL_USER, SQL_PASSWORD, ES_USERNAME, ES_PASSWORD, LLM_API_KEY | operator, from `secrets.env` | admin, its jobs; portal reads SQL_USER and SQL_PASSWORD only |
| `alerts-bi-registry` | ConfigMap with `teams.json` | operator | admin, its jobs |
| `alerts-bi-admin-secrets` | Secret: ADMIN_SECRET | operator, generated | admin container |
| `alerts-bi-ca-bundle` | optional ConfigMap with `ca-bundle.crt` | operator, for a private CA | admin, its jobs, portal (mounted at `/etc/alerts-bi-ca`) |
| `alerts-bi-portal` | Deployment, Service, Route, ServiceAccount | `portal.yaml` | readers |
| `alerts-bi-portal-sql` | optional Secret: SQL_USER, SQL_PASSWORD | operator, for a reader-only login | portal |

Preserve the current namespace, SQL database/login and registry. Build immutable admin and
portal image digests from the reviewed revisions; use the same admin digest for web, migration,
weekly and one-off jobs (one `ADMIN_IMAGE` value renders all of them). Admin has no dependency
on the retired runs package/image/repository. Apply migrations before consumers.

| Settings | Consumers and meaning |
|---|---|
| SQL_HOST, SQL_PORT, SQL_USER, SQL_PASSWORD, SQL_DATABASE, SQL_ENCRYPT, SQL_TRUST_SERVER_CERTIFICATE, SQL_REQUEST_TIMEOUT_MS | Admin, its jobs and the portal use the one existing database |
| ES_URL, ES_V1_INDEX, ES_V2_INDEX, ES_USERNAME, ES_PASSWORD, ES_CA_CERT, ES_REQUEST_TIMEOUT_MS, ES_PAGE_SIZE, ES_PIT_KEEP_ALIVE, ES_SEARCH_RETRIES, ES_SEARCH_RETRY_BACKOFF_MS | Admin, run worker and weekly execution only |
| LLM_ENABLED, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, LLM_MODEL_REVISION, LLM_TIMEOUT_MS, LLM_MAX_BATCH_SIZE, LLM_MAX_COMPLETION_TOKENS | Admin, run worker and weekly execution only; unchanged model configuration |
| SSL_CERT_FILE | Optional; set to the mounted CA bundle when the model endpoint uses a private CA (the model client honours it). Never set but empty |
| LOG_LEVEL | debug, info, warn or error; default info |
| ADMIN_HOST=0.0.0.0, ADMIN_PORT=8200 | Set by the template on the admin container (amended 2026-10-19: no login, so the console listens on the pod address) |
| ADMIN_SECRET | Private random value of at least 32 characters, signs the CSRF tokens (bound to the day only since 2026-10-19) |
| ADMIN_REGISTRY_PATH=/etc/alerts-bi/teams.json | Set by the templates on every admin container and job, so commands without `--registry` read the mounted registry, never the packaged mock one |
| ADMIN_DATABASE | Amended 2026-10-19: must stay unset in a deployment (it was an optional UI/worker/migration override). The weekly job, `weekly-status` and the portal always use SQL_DATABASE, so a different value would split the data; an upgrade that finds a different value stops until the database owner names the real database |
| PORTAL_HOST=0.0.0.0, PORTAL_PORT=8100, PORTAL_ALLOWED_NETWORKS, PORTAL_PAGE_SIZE | Set by the portal template; an upgrade carries over the existing reader allowlist and page size |

Keep SQL/ES/model credentials in `alerts-bi-secrets` and non-secret values in
`alerts-bi-config`; keep the registry in `alerts-bi-registry` and the admin form secret separate.
Portal requires only its SQL/reader settings, no registry, engine, operations or ES/model settings.
Validate an edited registry with `alerts-bi-admin registry check --registry <path>` before
installing it, increment registry_version, and preserve immutable snapshots in stored runs. Do
not use --dev-user on a deployment. Obsolete keys (ADMIN_OUT_DIR, API_OUT_DIR, API_*,
PORTAL_DATABASE, PORTAL_SQL_*) are removed from the ConfigMap on upgrade.

## Automatic migrations

Accepted 2026-10-13: the admin Deployment runs `db setup` in a regular `migrate` init container
before the UI and manual worker start. It uses the same admin image digest, SQL settings
and credentials, selecting ADMIN_DATABASE when nonempty and otherwise SQL_DATABASE (the CLI
default is alerts_bi_dev); deployments leave ADMIN_DATABASE unset. It creates SQL_DATABASE
when absent, then applies only missing revisions after checking the immutable ledger; current
head is 019_erase_operator_names. No data reset occurs. Before every rollout the runbook lists
pending revisions with `db status` from the new image; any pending revision passes the
migration gate below.

015_case_sensitive_applications (2026-10-18) rebuilds the primary keys and indexes on every
stored `application` column in one transaction. A failure rolls back completely. Its duration
grows with the stored findings, verdicts and read models, and its schema locks make portal
reads of the eight refreshed views, and every query on the seven tables, wait until it
commits. Once it is applied, an earlier admin image's `migrate` init container stops with
`Can't locate revision identified by '015_case_sensitive_applications'`, and the revision
refuses to downgrade. Restoring the database is then the only way back to an earlier image.
Before any rollout containing unapplied revisions 015 to 019 (the migration gate; the
database owner confirms each item, because the deploying agent cannot):

1. Take one database backup before the first pending revision and confirm that it restores.
   Keep the matching image, manifests and validator-compatible registry for recovery.
2. Record row counts for `alert_findings`, `llm_verdicts`, `run_alert_list`, `runs`,
   `daily_metrics`, `daily_rule_counts`, `review_publications`, `finding_decisions`,
   `weekly_review_log`, `run_jobs` and `run_backfills`, plus transaction-log free space.
   Size the window and log capacity for every pending revision, including index rebuilds:
   015, 016 and 018 each rewrite `alert_findings` and `llm_verdicts`; 017 changes all
   eight team-bearing tables listed below. Rehearse against a restored copy of comparable size.
3. Schedule a maintenance window outside the weekly job's schedule. Tell portal readers that
   pages may wait during the window.
4. Choose how the migration runs: the init container (default), or the maintenance Job with
   the console and scheduler stopped.

Nothing cuts the rebuild short. The migration connection sets no SQL query timeout:
`SQL_REQUEST_TIMEOUT_MS` applies to application queries, not to `db setup`. Neither the admin
Deployment nor the admin Job template sets `activeDeadlineSeconds`. Keep it that way, and add
no init-container deadline shorter than the sized window. If the pod is killed anyway, 015
rolls back and the next start repeats the whole lock window. When the sizing suggests a long
rebuild, choose the maintenance Job in item 4: stop the UI and worker and run `db setup` with
`admin-job.yaml` by hand.

016_case_sensitive_key_fields (2026-10-18) does the same for every stored `key_field`
column, in its own transaction: it rebuilds pk_alert_findings, pk_llm_verdicts and the
decision and alert-list indexes that contain `key_field`, then refreshes portal_alerts,
portal_alert_list and portal_decisions. Portal reads of those views and every query on
`alert_findings`, `llm_verdicts`, `finding_decisions` and `run_alert_list` wait until it
commits. It is roll-forward only in the same way: an image without 016 stops with `Can't
locate revision identified by '016_case_sensitive_key_fields'`, and the revision refuses to
downgrade. The checklist above and the rule that nothing cuts the rebuild short apply to it
too. When one image brings 015 and 016, one backup and one window cover both, but the window
must allow for `alert_findings` and `llm_verdicts` being rewritten twice. If 016 fails or the
pod is killed during it, 015 stays committed, 016 rolls back, and the next start repeats only
016's lock window.

017_case_sensitive_teams (2026-10-18) changes `team_id` to `Latin1_General_100_BIN2`
in `runs`, `daily_metrics`, `daily_rule_counts`, `review_publications`, `finding_decisions`,
`weekly_review_log`, `run_jobs` and `run_backfills`. In one transaction it drops dependent
automatic column statistics and seven team indexes, changes the eight columns, recreates
those indexes and refreshes dependent portal views. Readers and writers can wait for its
schema locks. Apply the same backup, sizing, maintenance-window and no-deadline rules to
017. One backup and window can cover 015–017, but budget for all pending revisions. If 017
fails, its changes roll back together; 015 and 016 remain committed and initialization
retries 017. A failed migration must fail readiness; never stamp past it.

An admin image without revision 017 cannot start its migration init container against a
017 database (`Can't locate revision identified by '017_case_sensitive_teams'`). Downgrade
is deliberately refused because case-distinct teams may now coexist. Recovery uses either
a forward-compatible image that still knows 017 and preserves binary comparisons and the
URL codec, or a verified pre-migration database backup restored with its matching old image,
manifests and registry. Restoring a backup discards writes made after that backup; stop
writers and obtain explicit recovery authorization before doing so. An image-and-registry
rollback alone is insufficient. Deploy admin and the shared 0.1.8 portal before admitting
expanded team IDs; do not roll back the portal while encoded links remain in use.

018_alert_identity_hash (2026-10-19) keys `alert_findings` and `llm_verdicts` by a
fixed-width hash, so identities wider than the old 900-byte clustered keys can be stored.
Until it is applied, a run with an identity whose `application` and `key_field` together
exceed 384 characters fails with SQL error 1946 and persists nothing. In one transaction it
drops pk_alert_findings and pk_llm_verdicts, adds a persisted computed `identity_hash` to both
tables, which computes a SHA-256 for every existing row, and rebuilds both clustered keys on
it. It refreshes no view. Every query on the two tables, and portal reads of the views over
`alert_findings`, wait until it commits. Apply the same backup, sizing, maintenance-window and
no-deadline rules to 018, sizing for one more rewrite of both tables. If 018 fails, it rolls
back completely, 015–017 remain committed, and initialization retries 018.

018 is roll-forward only like 015–017. An admin image without it stops its migration init
container with `Can't locate revision identified by '018_alert_identity_hash'`, and the
revision refuses to downgrade because wider identities may then be stored. Recovery uses a
forward-compatible image that knows 018, or the verified pre-migration backup restored with
its matching image and manifests, under the same authorization rules as 017.

Keep one replica and Recreate: an upgrade terminates the old UI/worker before creating the
new pod. A migration failure keeps the application containers unstarted and OpenShift retries
initialization. Inspect `oc logs <pod> -c migrate` and the pod's init-container status; fix
the database/schema problem and let initialization retry. Never reset-test, downgrade or stamp
over drift. The configured SQL login must retain schema-migration permissions and connectivity:
it connects to `master` to check that SQL_DATABASE exists, needs `dbcreator` only when the
database does not exist yet, and needs `db_owner` (schema changes) on SQL_DATABASE.

No separate migration launch is needed for normal rollout. Do not run the maintenance Job
concurrently with the Deployment, or change this topology to multiple/rolling pods without
coordinating migration execution. Manual pod deletion is not the Recreate upgrade ordering
guarantee; confirm prior writer sessions have ended before maintenance recreation. Every run
writer must use the updated image: on the first upgrade the runs-era trigger is stopped before
migrations and its JSON callers move to the console's `POST /run-jobs`.

## Admin console access

Amended 2026-10-19 (design "Admin console without login"): the console has no login proxy,
OAuth client, access Role or client allowlist. The Route uses edge TLS (HTTP redirects to HTTPS)
to a Service on port 8200, and the console listens on the pod address. Anyone who has the URL
and can reach the router may use every function, including Delete run; the URL is the only
barrier. No route returns 401, and no action records a person. Forms and JSON writes still need
the same-site CSRF token signed with `ADMIN_SECRET`. The first upgrade to this release deletes
the former proxy's objects (the `oauth-proxy` container, the `alerts-bi-admin-access` Role and
RoleBinding, the `alerts-bi-admin-tls` Secret, `COOKIE_SECRET`, the ServiceAccount's OAuth
redirect annotation and the Route allowlist annotations), because `oc apply` would keep them.

Keep one UI worker, one replica and one dedicated manual worker. Browser submissions and status
checks return promptly; analysis duration is independent of the Route timeout. The
compatibility POST /runs is still synchronous and retains its existing timeout considerations.

Probes: the admin container's readiness and liveness probes are TCP checks on port 8200, which
show only that the listener accepts connections. `/healthz` is not a probe, because it also
needs Elasticsearch, and an Elasticsearch outage must not take the publish, withdraw and decide
pages offline; the runbook checks `/healthz` through the Route. The worker has no probe.
Containers run with the restricted security context (no privilege escalation, all capabilities
dropped, RuntimeDefault seccomp) and memory limits; the analysis containers' 2 GiB default limit
leaves headroom over the 233 MiB peak measured for 1.2 million events and 20,000 identities.
Keep resource limits appropriate to the existing deployment: an upgrade keeps larger running
limits. The portal Route keeps both the `ip_allowlist` and the older `ip_whitelist` annotation,
so every 4.x router applies its client allowlist.

Runs write no files, so the application needs no writable output volume; CSV downloads render
from SQL. Earlier images write files after every run, so a rollback to those images also needs
the previous manifests (ADMIN_OUT_DIR, the output emptyDir mounts and the weekly
`--out /app/out/weekly`). This is subject to the database restore requirements for revisions
015–019 above; changing manifests alone cannot roll back a migrated database. CSVs left on an
old emptyDir are discarded with its pod; no cleanup is needed. Keep the image/registry access
policies appropriate to the existing deployment.

## One weekly scheduler

Transfer the existing CronJob after suspending it and draining its active Job. Use the same
admin image digest, registry, SQL settings and UTC clock. The command retains its SQL schedule
lock, due-week planning, health holds and publication rules. Failed/held work makes the Job fail;
monitor it and inspect `alerts-bi-admin weekly-status`. Do not introduce a scheduler per team.

The template creates `alerts-bi-weekly` suspended (`WEEKLY_SUSPEND=true`). Enable only one
scheduler, after the admin verification and `weekly --dry-run`, by setting
`WEEKLY_SUSPEND=false` and re-applying; old schedulers stay suspended until they are deleted.
The scheduled SQL lock is not a global manual-run lock; preserve the existing operational
avoidance of overlapping manual and scheduled execution for the same team/window.

## Portal and operational limits

The portal Deployment/Service/Route are built from `portal.yaml`; an existing portal under
another name is replaced only after the new one is verified. Its command is
`alerts-bi-portal`; its shared wheel is produced by admin and has no engine or operations
imports. It queries only portal_* views using the existing SQL settings. The SQL credential may
have broader rights, so retain reader network isolation and query/import boundaries: the
Route's client allowlist filters readers' addresses, and `PORTAL_ALLOWED_NETWORKS` admits the
router, loopback and the cluster networks the application sees behind it. Its probes are TCP
checks on port 8100, which need no SQL and bypass the client allowlist. A login in the
`alerts_bi_reader` role, created by the database-owning team, can replace the portal's
SQL_USER and SQL_PASSWORD through the portal-only Secret `alerts-bi-portal-sql`
(`PORTAL_SQL_SECRET`) without code changes; see the 2026-10-17 design note on the portal SQL
login.

SQL_ENCRYPT=true requires TLS on every admin, migration, job and portal SQL connection (shared
0.1.7 with the locked pymssql 2.4.4; see the 2026-10-17 design amendment). It is false by
default, and only true/false spellings are accepted. Before enabling it, confirm the SQL Server
accepts encrypted connections. A server that does not will stop the migration init container,
admin, portal, workers and CronJobs from connecting. The server certificate is not validated,
so TLS does not stop an active interceptor. Keep SQL_TRUST_SERVER_CERTIFICATE=true: false is
refused before any SQL connection. Web apps fail at startup, and the init container, workers
and CronJobs fail at their first connection. Before enabling encryption, confirm the live
configuration does not set SQL_TRUST_SERVER_CERTIFICATE=false. With encryption off, each
process logs `sql.unencrypted` once for each SQL server. Preserve the approved database
network protections either way.

Enable encryption in this order:

1. Before rolling out shared 0.1.7 images, check the live Secret and ConfigMap. SQL_ENCRYPT
   must be a true/false spelling, because any other value now stops startup.
   SQL_TRUST_SERVER_CERTIFICATE must not be false while SQL_ENCRYPT is true.
2. Roll out the new images with SQL_ENCRYPT=false and observe a normal cycle.
3. Confirm the SQL Server completes a TLS handshake from the cluster network.
4. Set SQL_ENCRYPT=true. The migration init container is the first encrypted connection and is
   a no-op when the schema is current. Then confirm admin and portal pages load, and run one
   worker job and one CronJob manually (`oc create job --from=cronjob/alerts-bi-weekly <job-name>`).
5. Rollback: images built before shared 0.1.7 ignore SQL_ENCRYPT and connect in plaintext.
   Set SQL_ENCRYPT=false when rolling back, so the configuration does not claim encryption.

Validate that the console opens without a login and names nobody, health through the Route,
run navigation, a permitted test execution, publication, decisions, withdrawal, exports and
portal visibility, then observe a
scheduled cycle. Follow the retirement document for old-trigger removal, rollback and archival.
No commands in this document have been executed on a cluster by this change.
