# OpenShift deployment: unified admin and read-only portal

Current topology, approved 2026-10-12; amended 2026-10-17 to remove run output files and their
mounts. Examples are unexecuted rollout preparation, not cluster
validation. Admin is the only operator/analysis image. The separate portal remains read-only.
See [retirement cutover and rollback](runs-retirement.md) before changing existing deployments.

## Prerequisites and configuration

Preserve the current namespace, SQL database/login and registry. Build immutable admin and
portal image digests from the reviewed revisions; use the same admin digest for web, migration
and weekly jobs. Admin has no dependency on the retired runs package/image/repository.
Apply migrations once, before consumers. No new schema revision is introduced by consolidation.

The admin checkout contains `deploy/admin.yaml`, `deploy/migrate.yaml` and `deploy/weekly.yaml`.
Replace ADMIN_IMAGE_DIGEST and OAUTH_PROXY_IMAGE_DIGEST with verified immutable images;
replace namespace/group/network examples with the approved deployment's existing values.
Do not commit generated secrets or apply the placeholder manifests unchanged.

| Settings | Consumers and meaning |
|---|---|
| SQL_HOST, SQL_PORT, SQL_USER, SQL_PASSWORD, SQL_DATABASE, SQL_REQUEST_TIMEOUT_MS | Admin, migration/weekly jobs and portal use the existing database |
| ES_URL, ES_USERNAME, ES_PASSWORD, ES_CA_CERT, ES_REQUEST_TIMEOUT_MS, ES_PAGE_SIZE | Admin and weekly execution only |
| LLM_ENABLED, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, LLM_MODEL_REVISION, LLM_TIMEOUT_MS, LLM_MAX_BATCH_SIZE, LLM_MAX_COMPLETION_TOKENS | Admin and weekly execution only; unchanged model configuration |
| ADMIN_HOST=127.0.0.1, ADMIN_PORT=8200, ADMIN_USER_HEADER=X-Forwarded-User | Authenticated admin listener only |
| ADMIN_SECRET | Private random value of at least 32 characters, signs actor-bound CSRF tokens |
| ADMIN_DATABASE | Optional UI/worker/migration database override; unset uses SQL_DATABASE |
| ADMIN_REGISTRY_PATH or --registry | Mounted admin-owned registry at /etc/alerts-bi/teams.json |
| COOKIE_SECRET | Existing proxy session secret; keep its supported encoding and rotation procedure |
| PORTAL_HOST, PORTAL_PORT, PORTAL_ALLOWED_NETWORKS | Existing standalone reader configuration, port 8100 |

Keep SQL/ES/model credentials in the existing Secret and nonsecret values in
`alerts-bi-config`; keep the registry in `alerts-bi-registry` and proxy/admin secrets separate.
Portal requires only its SQL/reader settings, no registry, engine, operations or ES/model settings.
Validate edited registry with `alerts-bi-admin registry check --registry <path>`, increment
registry_version, and preserve immutable snapshots in stored runs. Do not use --dev-user on a deployment.

## Automatic migrations

Accepted 2026-10-13: the admin Deployment now runs `db setup` in a regular `migrate`
init container before the UI, manual worker and proxy start. It uses the same admin image
digest, SQL settings and credentials, selecting ADMIN_DATABASE when nonempty and otherwise
SQL_DATABASE (the CLI default is alerts_bi_dev). It applies only missing revisions after
checking the immutable ledger; current head is 018_alert_identity_hash. No data reset
occurs.

015_case_sensitive_applications (2026-10-18) rebuilds the primary keys and indexes on every
stored `application` column in one transaction. A failure rolls back completely. Its duration
grows with the stored findings, verdicts and read models, and its schema locks make portal
reads of the eight refreshed views, and every query on the seven tables, wait until it
commits. Once it is applied, an earlier admin image's `migrate` init container stops with
`Can't locate revision identified by '015_case_sensitive_applications'`, and the revision
refuses to downgrade. Restoring the database is then the only way back to an earlier image.
Before any rollout containing unapplied revisions 015, 016, 017 or 018:

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

Nothing cuts the rebuild short. The migration connection sets no SQL query timeout:
`SQL_REQUEST_TIMEOUT_MS` applies to application queries, not to `db setup`. The admin
Deployment sets no `activeDeadlineSeconds`. Keep it that way, and add no init-container deadline
shorter than the sized window. If the pod is killed anyway, 015 rolls back and the next start
repeats the whole lock window. When the sizing suggests a long rebuild, stop the UI and worker
and run deploy/migrate.yaml by hand instead.

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
over drift. The configured SQL login must retain schema-migration permissions and connectivity.

Build the updated image, replace all ADMIN_IMAGE_DIGEST occurrences with its same immutable
digest, and apply deploy/admin.yaml. No separate migration launch is needed for normal rollout.
Do not launch the optional job below concurrently or change this topology to multiple/rolling
pods without coordinating migration execution. Manual pod deletion is not the Recreate upgrade
ordering guarantee; confirm prior writer sessions have ended before maintenance recreation.

### Optional manual maintenance job

deploy/migrate.yaml remains for controlled maintenance with UI/worker writers stopped. It
uses the same database selection as initialization. It is not part of normal automatic rollout.

```yaml
apiVersion: batch/v1
kind: Job
metadata: {name: alerts-bi-migrate}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: ADMIN_IMAGE_DIGEST
          command: ["/bin/sh", "-ec"]
          args:
            - 'exec alerts-bi-admin db setup --database "${ADMIN_DATABASE:-${SQL_DATABASE:-alerts_bi_dev}}"'
          envFrom:
            - configMapRef: {name: alerts-bi-config}
            - secretRef: {name: alerts-bi-secrets}
```

## Authenticated admin

Preserve the existing oauth-proxy identity, authorization, cookie and TLS configuration. The
Service targets only proxy port 8443; no Service exposes application port 8200. Keep one UI worker,
one replica and one dedicated manual worker. Browser submissions and status checks return promptly;
analysis duration is independent of the Route timeout. The compatibility POST /runs is still
synchronous and retains its existing timeout considerations. Configure health probes through a trusted
authenticated path; an unauthenticated /healthz request correctly returns 401.

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: alerts-bi-admin
  annotations:
    serviceaccounts.openshift.io/oauth-redirectreference.admin: '{"kind":"OAuthRedirectReference","apiVersion":"v1","reference":{"kind":"Route","name":"alerts-bi-admin"}}'
---
apiVersion: v1
kind: Secret
metadata: {name: alerts-bi-admin-secrets}
stringData:
  ADMIN_SECRET: "..."          # 32+ random characters; signs the admin forms
  COOKIE_SECRET: "..."         # 32 random bytes, base64; the proxy's session cookie
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: alerts-bi-admin}
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector: {matchLabels: {app: alerts-bi-admin}}
  template:
    metadata: {labels: {app: alerts-bi-admin}}
    spec:
      serviceAccountName: alerts-bi-admin
      initContainers:
        - name: migrate
          image: ADMIN_IMAGE_DIGEST
          command: ["/bin/sh", "-ec"]
          args:
            - 'exec alerts-bi-admin db setup --database "${ADMIN_DATABASE:-${SQL_DATABASE:-alerts_bi_dev}}"'
          envFrom:
            - configMapRef: {name: alerts-bi-config}
            - secretRef: {name: alerts-bi-secrets}
      containers:
        - name: admin
          image: ADMIN_IMAGE_DIGEST
          command: ["alerts-bi-admin", "serve", "--registry", "/etc/alerts-bi/teams.json"]
          env:
            - {name: ADMIN_HOST, value: "127.0.0.1"}
            - {name: ADMIN_PORT, value: "8200"}
          envFrom:
            - configMapRef: {name: alerts-bi-config}
            - secretRef: {name: alerts-bi-secrets}
            - secretRef: {name: alerts-bi-admin-secrets}
          volumeMounts:
            - {name: registry, mountPath: /etc/alerts-bi, readOnly: true}
        - name: run-worker
          image: ADMIN_IMAGE_DIGEST
          command: ["alerts-bi-admin", "run-worker", "--registry", "/etc/alerts-bi/teams.json"]
          envFrom:
            - configMapRef: {name: alerts-bi-config}
            - secretRef: {name: alerts-bi-secrets}
          volumeMounts:
            - {name: registry, mountPath: /etc/alerts-bi, readOnly: true}
        - name: oauth-proxy
          image: OAUTH_PROXY_IMAGE_DIGEST
          args:
            - --provider=openshift
            - --https-address=:8443
            - --http-address=
            - --upstream=http://127.0.0.1:8200
            - --openshift-service-account=alerts-bi-admin
            - --tls-cert=/etc/tls/private/tls.crt
            - --tls-key=/etc/tls/private/tls.key
            - --cookie-secret-file=/etc/proxy/secrets/COOKIE_SECRET
            - '--openshift-sar={"namespace":"alerts-bi","resource":"services","resourceName":"alerts-bi-admin","verb":"get"}'
          ports: [{containerPort: 8443, name: https}]
          volumeMounts:
            - {name: proxy-tls, mountPath: /etc/tls/private}
            - {name: proxy-secrets, mountPath: /etc/proxy/secrets}
      volumes:
        - {name: registry, configMap: {name: alerts-bi-registry}}
        - {name: proxy-tls, secret: {secretName: alerts-bi-admin-tls}}
        - {name: proxy-secrets, secret: {secretName: alerts-bi-admin-secrets}}
---
apiVersion: v1
kind: Service
metadata:
  name: alerts-bi-admin
  annotations:
    service.beta.openshift.io/serving-cert-secret-name: alerts-bi-admin-tls
spec:
  selector: {app: alerts-bi-admin}
  ports: [{name: https, port: 443, targetPort: 8443}]
---
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: alerts-bi-admin
  annotations:
    haproxy.router.openshift.io/timeout: "30m"
    haproxy.router.openshift.io/ip_allowlist: "10.0.0.0/8 172.16.0.0/12 192.168.0.0/16"
spec:
  to: {kind: Service, name: alerts-bi-admin}
  tls: {termination: reencrypt}
---
# Only the standardization team passes the proxy's access check.
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: alerts-bi-admin-access}
rules:
  - apiGroups: [""]
    resources: [services]
    resourceNames: [alerts-bi-admin]
    verbs: [get]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: alerts-bi-admin-access}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: Role, name: alerts-bi-admin-access}
subjects:
  - {apiGroup: rbac.authorization.k8s.io, kind: Group, name: standardization-team}
```

Runs write no files, so the application needs no writable output volume; CSV downloads render
from SQL. Apply these manifests with the image that contains this change, removing any existing
ADMIN_OUT_DIR setting, output mounts and the weekly `--out`. Earlier images write files after
every run, so a rollback to those images also needs the previous manifests (ADMIN_OUT_DIR,
the output emptyDir mounts and the weekly `--out /app/out/weekly`). This is subject to the
database restore requirements for revisions 015–018 above; changing manifests alone cannot
roll back a migrated database. CSVs left on an old emptyDir
are discarded with its pod; no cleanup is needed. Keep
the image/registry access policies and resource limits appropriate to the existing deployment.

## One weekly scheduler

Transfer the existing CronJob after suspending it and draining its active Job. Use the same
admin image digest, registry, SQL settings and UTC clock. The command retains its SQL schedule
lock, due-week planning, health holds and publication rules. Failed/held work makes the Job fail;
monitor it and inspect `alerts-bi-admin weekly-status`. Do not introduce a scheduler per team.

```yaml
apiVersion: batch/v1
kind: CronJob
metadata: {name: alerts-bi-weekly}
spec:
  suspend: true # enable only after the old scheduler is suspended and drained
  schedule: "30 2 * * *"
  timeZone: "Etc/UTC"
  concurrencyPolicy: Forbid
  startingDeadlineSeconds: 3600
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 10
  jobTemplate:
    spec:
      backoffLimit: 0
      activeDeadlineSeconds: 43200
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: weekly
              image: ADMIN_IMAGE_DIGEST
              command: ["alerts-bi-admin", "weekly", "--registry", "/etc/alerts-bi/teams.json"]
              envFrom:
                - configMapRef: {name: alerts-bi-config}
                - secretRef: {name: alerts-bi-secrets}
              volumeMounts:
                - {name: registry, mountPath: /etc/alerts-bi, readOnly: true}
          volumes:
            - {name: registry, configMap: {name: alerts-bi-registry}}
```

The example starts suspended. Enable only one scheduler after verifying `weekly --dry-run` and
the cutover checks. The scheduled SQL lock is not a global manual-run lock; preserve the existing
operational avoidance of overlapping manual and scheduled execution for the same team/window.

## Portal and operational limits

Keep the independently built portal Deployment/Service/Route and its allowlist. Its command is
`alerts-bi-portal`; its shared wheel is produced by admin and has no engine or operations imports.
It queries only portal_* views using the existing SQL settings. The SQL credential may have
broader rights, so retain reader network isolation and query/import boundaries. A login in
the `alerts_bi_reader` role, created by the database-owning team, can replace the portal's
SQL_USER and SQL_PASSWORD through a portal-only Secret without code changes; see the
2026-10-17 design note on the portal SQL login.

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

Validate login denial, authenticated health, run navigation, a permitted test execution,
publication, decision attribution, withdrawal, exports and portal visibility, then observe a
scheduled cycle. Follow the retirement document for old-trigger removal, rollback and archival.
No commands in this document have been executed on a cluster by this change.
