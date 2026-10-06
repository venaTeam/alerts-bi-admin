# Deploying Alerts BI to OpenShift

**Last updated:** 2026-09-24

**Status: proposed, not proven.** Nothing here has been exercised on a cluster. Kubernetes
deployment is deferred post-MVP work (design section 7.4); the weekly schedule of design
section 7.11 is built but its CronJob below is untested. The repository carries no
Dockerfile, no manifest and no CI, and everything below is new.

Read it in two halves. The **configuration reference** is grounded in the code: every
variable was checked against what the application actually reads, and the ones that are read
but never applied are called out as gaps. The **Dockerfile and manifests** are a starting
point to review, not something known to run.

Credentials here are placeholders. Real values belong in an OpenShift Secret and never in
this repository.

Read [Known gaps](#known-gaps) before exposing this to anyone.

---

## 1. What has to exist before you start

| Dependency | Requirement |
|---|---|
| Elasticsearch | Reachable from the pod network. Read-only: the pipeline never writes to it. Indices `appchi-v1` and `appchi-v2` |
| SQL Server | A database the app owns. It is the store the design cannot skip: a run that cannot persist has not done its job |
| On-prem model | An OpenAI-compatible endpoint, if you want LLM assessment. Optional — the deterministic pipeline runs without it |
| Image registry | Somewhere to push the image. OpenShift's internal registry is fine |

A least-privilege SQL account needs `CREATE TABLE` for the migration Job, and
`SELECT/INSERT/UPDATE/DELETE` on the eight tables at runtime. The migration Job additionally
needs `CREATE DATABASE` **only** if you let it create the database; create it yourself and
that permission is unnecessary.

---

## 2. Four files the image must contain

This is the part most likely to be missed, because these are read from disk at runtime,
relative to the **working directory** — not from the installed package.

| Path (relative to WORKDIR) | Read by | If missing |
|---|---|---|
| `docs/Alerting_Guide_Appchi_EN.md` | LLM prompt builder | **Every LLM call fails.** The guides go into the system prompt verbatim |
| `docs/what_is_an_incorrect_alert_EN.md` | LLM prompt builder | Same |
| `config/teams.schema.json` | Registry validation | **Every run fails**, including deterministic ones. The path is not configurable |
| `config/teams.json` | Registry | Every run fails, unless `API_REGISTRY_PATH` points elsewhere |

The SQL migrations are *not* in this list: they ship inside the wheel at
`src/db/migrations/`, verified by inspecting a built wheel. The migration Job needs no
mounted files.

---

## 3. Image

No Dockerfile exists. This one is untested — build it and check the four paths above.

```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

# Dependencies first, from the committed lockfile, so a code change does not re-resolve.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
# Read at runtime relative to the working directory, not imported from the package.
COPY config/teams.schema.json config/teams.json config/
COPY docs/Alerting_Guide_Appchi_EN.md docs/what_is_an_incorrect_alert_EN.md docs/
# `alerts-bi db migrate` builds its Alembic config in code and does not need this; it is
# here so `alembic history` and `alembic heads` work when you shell into the pod.
COPY alembic.ini ./

RUN uv sync --frozen --no-dev

# OpenShift's restricted-v2 SCC runs the container as an arbitrary UID in group 0, so
# everything the app touches must be group-accessible rather than owned by a fixed user.
RUN mkdir -p /app/out && chgrp -R 0 /app && chmod -R g=u /app

USER 1001
EXPOSE 8000
CMD ["alerts-bi", "serve"]
```

Two OpenShift-specific points:

- **Arbitrary UID.** Do not assume UID 1001 at runtime; OpenShift substitutes one from the
  project's range. The `chgrp 0` / `chmod g=u` lines are what make that work.
- **`pymssql`** ships manylinux wheels with FreeTDS bundled, so `slim` should suffice. If the
  build or first connection fails on a missing shared library, add
  `RUN apt-get update && apt-get install -y --no-install-recommends libssl3 && rm -rf /var/lib/apt/lists/*`.

---

## 4. Configuration reference

Every variable below was verified against the code. Anything not listed here is not read.

### Secret

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: alerts-bi-secrets
type: Opaque
stringData:
  SQL_PASSWORD: "..."
  ES_PASSWORD: "..."        # omit if Elasticsearch is unauthenticated
  LLM_API_KEY: "..."        # omit if the endpoint needs no key
```

| Variable | Notes |
|---|---|
| `SQL_PASSWORD` | Required |
| `ES_PASSWORD` | Only used when `ES_USERNAME` is set |
| `LLM_API_KEY` | If unset, the SDK is given a placeholder — fine for an endpoint that ignores it |

### ConfigMap

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: alerts-bi-config
data:
  # ---- the surface ----
  API_HOST: "0.0.0.0"          # see below; the default will not work in a pod
  API_PORT: "8000"
  API_REGISTRY_PATH: "/etc/alerts-bi/teams.json"
  API_OUT_DIR: "/app/out"
  LOG_LEVEL: "info"

  # ---- Elasticsearch ----
  ES_URL: "https://elasticsearch.example.svc:9200"
  ES_USERNAME: "alerts-bi"
  ES_CA_CERT: "/etc/alerts-bi/ca/ca.crt"
  ES_PAGE_SIZE: "1000"
  ES_REQUEST_TIMEOUT_MS: "60000"

  # ---- SQL Server ----
  SQL_HOST: "mssql.example.svc"
  SQL_PORT: "1433"
  SQL_USER: "alerts_bi"
  SQL_DATABASE: "alerts_bi"
  SQL_REQUEST_TIMEOUT_MS: "60000"

  # ---- on-prem model ----
  LLM_ENABLED: "true"
  LLM_BASE_URL: "https://llm.example.svc/v1"
  LLM_MODEL: "your-exact-deployment-id"
  LLM_TIMEOUT_MS: "120000"
  LLM_MAX_BATCH_SIZE: "200"
```

**`API_HOST` must be `0.0.0.0`.** It defaults to `127.0.0.1` on purpose — the surface has no
authentication, so binding wider is a deliberate act. In a pod, loopback means the Service
reaches nothing and the pod looks healthy while serving no one.

**`ES_CA_CERT` is a path, not a certificate.** Mount the bundle and point at the file. It is
passed to the Elasticsearch client as `ca_certs` — verified.

**`LLM_ENABLED` must be `true`** or every eligible identity is recorded `unassessed` with a
reason, and the scorecard says plainly that nothing was examined. That is correct behaviour,
not a failure, which is exactly why it is easy to deploy and not notice.

**`LLM_BASE_URL` and `LLM_MODEL` are required** when the model is enabled — the client
refuses to construct without them. `LLM_MODEL` is recorded as `model_version` on every run
and participates in the durable verdict cache key, so changing it invalidates reuse. That is
intended: a different model is a different judgement.

**`LLM_MAX_BATCH_SIZE` may lower 200, never raise it.** A higher value is rejected at
startup, before a run reads a single alert.

### Deliberately absent

| Variable | Why |
|---|---|
| `SQL_TEST_DATABASE` | Only the test suite uses it. Setting it in production points the guarded destructive reset at a real name |
| `LLM_LIVE_TEST` | Opt-in test flag |
| `SQL_ENCRYPT`, `SQL_TRUST_SERVER_CERTIFICATE` | **Read into config and never applied.** See [Known gaps](#known-gaps) |

---

## 5. Migrations

The application does not migrate itself. Run this before the Deployment, as a Job — or as an
init container if you would rather it re-check on every rollout.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: alerts-bi-migrate
spec:
  backoffLimit: 3
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: image-registry.openshift-image-registry.svc:5000/alerts-bi/alerts-bi:latest
          command: ["alerts-bi", "db", "migrate"]
          envFrom:
            - configMapRef: {name: alerts-bi-config}
            - secretRef: {name: alerts-bi-secrets}
```

Before the first rollout that includes `005_team_summary`, confirm the database's
compatibility level is 130 or higher, because 005 uses `STRING_SPLIT`:

```sql
SELECT compatibility_level FROM sys.databases WHERE name = DB_NAME();
```

It is safe to re-run: Alembic skips revisions already at or below the current head. A
migration whose file changed after being applied is a hard error rather than a silent
re-apply - that check is the `schema_migrations` ledger, not Alembic, which records only a
head revision. `alerts-bi db status` prints the current revision and each migration's
checksum state, and warns if the revision graph has more than one head.

An existing database that predates Alembic needs stamping once, so Alembic knows its schema
is already at head:

```bash
oc rsh deploy/alerts-bi python -c "from src.config import load_config; from src.db.migrate import stamp; c = load_config(); stamp(c.sql, c.sql.database)"
```

---

## 6. Deployment, Service, Route

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: alerts-bi
spec:
  replicas: 1                        # see Known gaps — do not raise this
  selector:
    matchLabels: {app: alerts-bi}
  template:
    metadata:
      labels: {app: alerts-bi}
    spec:
      containers:
        - name: alerts-bi
          image: image-registry.openshift-image-registry.svc:5000/alerts-bi/alerts-bi:latest
          ports:
            - containerPort: 8000
          envFrom:
            - configMapRef: {name: alerts-bi-config}
            - secretRef: {name: alerts-bi-secrets}
          volumeMounts:
            # NOT subPath — see section 7.
            - {name: registry, mountPath: /etc/alerts-bi}
            - {name: es-ca, mountPath: /etc/alerts-bi/ca, readOnly: true}
            - {name: reports, mountPath: /app/out}
          readinessProbe:
            httpGet: {path: /healthz, port: 8000}
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet: {path: /healthz, port: 8000}
            initialDelaySeconds: 15
            periodSeconds: 30
            failureThreshold: 6      # generous: a run holds the worker while it executes
          resources:
            requests: {cpu: 200m, memory: 512Mi}
            limits:   {cpu: "2",  memory: 2Gi}
      volumes:
        - name: registry
          configMap: {name: alerts-bi-registry}
        - name: es-ca
          configMap: {name: alerts-bi-es-ca}
        - name: reports
          emptyDir: {}
```

**On the probes.** `/healthz` opens a SQL connection and queries Elasticsearch, and returns
`503` when either is down. That is honest, but it means a database blip restarts the pod
under a liveness probe. Consider pointing **liveness** at `/` (which only reads the
registry) and keeping `/healthz` for **readiness**, so a dependency outage takes the pod out
of rotation without cycling it.

**On `resources`.** A run for the largest fixture team takes about 1.2 s and is
single-threaded; production volume will be larger. Size from your own data — these numbers
are a placeholder, not a measurement.

**On `/app/out`.** The four files are written there on every run, but nothing reads them
back: reports are re-rendered from committed SQL rows by `GET /runs/<run_id>`. An `emptyDir`
is fine. Use a PVC only if you want the files to survive a restart, and remember a PVC plus
more than one replica is another reason to stay at one.

Service and Route:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: alerts-bi
spec:
  selector: {app: alerts-bi}
  ports:
    - {name: http, port: 8000, targetPort: 8000}
```

**Do not create a plain Route until you have read the authentication gap below.**

---

## 7. The team registry

`config/teams.json` decides whose alerts count as whose. Mount it from its own ConfigMap:

```bash
oc create configmap alerts-bi-registry \
  --from-file=teams.json=config/teams.json \
  --dry-run=client -o yaml | oc apply -f -
```

with `API_REGISTRY_PATH=/etc/alerts-bi/teams.json`.

### Updating it needs no restart

The registry is read from disk on **every** request — there is no caching. Verified against
a running instance: editing the file on disk and calling `/teams` without restarting
returned the new team immediately.

So the update is:

```bash
# edit config/teams.json, bump registry_version, then:
oc create configmap alerts-bi-registry --from-file=teams.json=config/teams.json \
  --dry-run=client -o yaml | oc apply -f -
```

and the pod picks it up within the kubelet sync period, typically under a minute. No
rollout, no restart.

### Four things that will catch you out

1. **Do not mount it with `subPath`.** `subPath` mounts never receive ConfigMap updates. You
   would silently be back to needing a rollout for every ownership change.
2. **Propagation is not instant** — allow up to a minute, and confirm with `GET /teams`
   rather than assuming.
3. **ConfigMaps cap at 1 MiB.** The current registry is 5.6 KB, so there is room for roughly
   a hundred times the present team count.
4. **The schema stays in the image.** `config/teams.schema.json` is resolved relative to the
   working directory and is not configurable, so the mount must not shadow `/app/config`.
   Mounting the registry at `/etc/alerts-bi/` as above keeps them apart.

### Bump `registry_version` on every edit

Every run stores the registry version, the SHA-256 of the complete file, and a snapshot of
the selected entry. Without a bump, *"their numbers moved"* and *"we edited the mapping"*
stop being distinguishable after the fact — which is the whole reason the version exists.

### Validate before applying

A bad registry fails runs rather than degrading quietly, so catch it before it reaches the
cluster:

```bash
uv run python -c "from src.registry import load_registry; r = load_registry('config/teams.json'); print(f'ok: {r.registry_version}, {len(r.teams)} teams')"
```

The errors are specific — a duplicate operator names both claimants, an entry with no source
operator names itself.

### Consider keeping it in git instead

Mounting from a ConfigMap gives hot reload. Baking `teams.json` into the image gives code
review, history and rollback on a change that decides attribution — arguably worth more than
the convenience. A middle path is GitOps rendering the ConfigMap from the repository, which
gives both. Whatever you choose, the `registry_version` discipline still applies.

---

## 8. Known gaps

Fix or accept these deliberately. The first is the serious one.

### There is no authentication

Every route is open. Anyone who reaches the Service can trigger runs — which read
Elasticsearch and write SQL — and read every team's scorecard, including the per-alert work
list. The surface binds to loopback by default precisely because of this.

Options, roughly in order of preference:

1. **No Route at all.** Reach it with `oc port-forward` when someone needs to start a run.
   Costs nothing and closes the gap completely.
2. **An `oauth-proxy` sidecar** in front of the container, so OpenShift identity gates it.
   The usual pattern for internal tools on OpenShift.
3. **A Route plus a NetworkPolicy** restricting ingress to known sources. Weaker — anything
   inside the allowed range is trusted.

Do not put an unauthenticated Route on a shared cluster.

### The review portal and admin app

See [Running it without touching a pod](#running-it-without-touching-a-pod) for their manifests.

The read-only review portal (`alerts-bi portal`, design section 7.10) is a second process
and should be a second Deployment. It is meant to be reachable from the company network,
unlike the trigger surface. It uses the same `SQL_*` database and credential as the pipeline;
no portal-specific SQL login or startup permission audit is required. Its client-network
allowlist (`PORTAL_ALLOWED_NETWORKS`)
sees the router's address rather than the reader's, so on a cluster restrict it at the Route
or with a NetworkPolicy instead. Never expose the trigger surface through the portal's
Route. None of this has run on a cluster.

### `SQL_ENCRYPT` and `SQL_TRUST_SERVER_CERTIFICATE` do nothing

Both are read into configuration and never passed to the driver. Setting them will not
encrypt the connection. This predates the SQLAlchemy migration — the raw `pymssql` code did
not use them either — but it matters here, because in a cluster the database connection
crosses the pod network rather than a loopback socket.

Until it is wired through, treat the SQL connection as unencrypted and place the database
accordingly: same cluster, restricted by NetworkPolicy, or a service mesh providing mTLS.

### One replica only

The lock that serializes runs is a per-process `threading.Lock`. With two pods, two
concurrent requests for the same team and clock derive the **same** deterministic `run_id`
and race to replace each other's rows. Persistence is transactional so nothing corrupts, but
the work is wasted and the audit trail becomes confusing.

If you need more than one replica, the gate has to move into the database — a row lock or an
advisory lock keyed by `run_id`.

### A run occupies a worker for its whole duration

The run endpoint is synchronous by design, dispatched to FastAPI's thread pool. A long run
does not block other requests, but the HTTP connection stays open until it finishes. Set
Route and client timeouts above your slowest expected run, or the caller will see a gateway
timeout while the run completes and persists correctly anyway — the result is still
retrievable at `GET /runs/<run_id>`.

### Running it without touching a pod

Everything routine runs by itself (design section 7.12). Apply these once; after that, the
only thing anyone edits is the registry ConfigMap, and the only screen anyone uses is the
admin app.

**Schema on every rollout** - add an init container to the app Deployment if its SQL login
can apply migrations. Otherwise, the database-owning team must apply them before rollout:

```yaml
      initContainers:
        - name: db-setup
          image: image-registry.openshift-image-registry.svc:5000/alerts-bi/alerts-bi:latest
          command: ["alerts-bi", "db", "setup"]
          envFrom:
            - configMapRef: {name: alerts-bi-config}
            - secretRef: {name: alerts-bi-secrets}
```

**The reader portal** - its own Deployment, Service and Route, using the same SQL
configuration and Secret as the pipeline:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: {name: alerts-bi-portal}
spec:
  replicas: 2                           # read-only and stateless; more than one is fine
  selector: {matchLabels: {app: alerts-bi-portal}}
  template:
    metadata: {labels: {app: alerts-bi-portal}}
    spec:
      containers:
        - name: portal
          image: image-registry.openshift-image-registry.svc:5000/alerts-bi/alerts-bi:latest
          command: ["alerts-bi", "portal"]
          env:
            - {name: PORTAL_HOST, value: "0.0.0.0"}
            - {name: PORTAL_PORT, value: "8100"}
            # The router is the client the allowlist sees; restrict readers at the Route.
            - {name: PORTAL_ALLOWED_NETWORKS, value: "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8"}
          envFrom:
            - configMapRef: {name: alerts-bi-config}
            - secretRef: {name: alerts-bi-secrets}
          ports: [{containerPort: 8100}]
          readinessProbe: {httpGet: {path: /healthz, port: 8100}}
---
apiVersion: v1
kind: Service
metadata: {name: alerts-bi-portal}
spec:
  selector: {app: alerts-bi-portal}
  ports: [{port: 8100, targetPort: 8100}]
---
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: alerts-bi-portal
  annotations:
    haproxy.router.openshift.io/ip_allowlist: "10.0.0.0/8 172.16.0.0/12 192.168.0.0/16"
spec:
  to: {kind: Service, name: alerts-bi-portal}
  tls: {termination: edge}
```

The portal queries only the `portal_*` views, but the shared SQL credential may have broader
rights. Keep the portal Route and client allowlist limited to the intended company network.

**The operator admin app** - a Deployment with OpenShift's `oauth-proxy` as a sidecar. The
proxy signs people in and admits only those allowed to `get` the Service below; the app binds
to the pod's loopback and trusts the proxy's `X-Forwarded-User`.

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
  selector: {matchLabels: {app: alerts-bi-admin}}
  template:
    metadata: {labels: {app: alerts-bi-admin}}
    spec:
      serviceAccountName: alerts-bi-admin
      containers:
        - name: admin
          image: image-registry.openshift-image-registry.svc:5000/alerts-bi/alerts-bi:latest
          command: ["alerts-bi", "admin", "--registry", "/etc/alerts-bi/teams.json"]
          envFrom:
            - configMapRef: {name: alerts-bi-config}
            - secretRef: {name: alerts-bi-secrets}
            - secretRef: {name: alerts-bi-admin-secrets}
          volumeMounts: [{name: registry, mountPath: /etc/alerts-bi}]
        - name: oauth-proxy
          image: registry.redhat.io/openshift4/ose-oauth-proxy:latest
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

Replace `standardization-team` with your OpenShift group and `alerts-bi` with your
namespace. The proxy passes the signed-in user's name as `X-Forwarded-User`, and every
publication, withdrawal and decision is recorded under it. The admin container listens only
on `127.0.0.1:8200`; there is deliberately no Service port for it.

After this, day to day:

| You want to | Do |
|---|---|
| Add a team | Add it to `config/teams.json` with `weekly_review.enabled`, run `alerts-bi registry check` locally, update the `alerts-bi-registry` ConfigMap. The next daily run starts it |
| See how the schedule is doing | The admin app's team list; failed `alerts-bi-weekly` Jobs in the console |
| Publish, withdraw, decide | The admin app |
| Upgrade | Push the image and roll out; the init container migrates first |

Nothing here has run on a cluster.

### Weekly reviews run from one CronJob

`alerts-bi weekly` is the whole schedule (design section 7.11). One CronJob covers every team
enrolled in the registry; adding a team never needs a new CronJob. Run it daily - it only
does what is due, so a missed day heals itself the next:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: alerts-bi-weekly
spec:
  schedule: "30 2 * * *"          # daily, 02:30 UTC; weeks end Monday 00:00 UTC
  timeZone: "Etc/UTC"
  concurrencyPolicy: Forbid
  startingDeadlineSeconds: 3600
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 10
  jobTemplate:
    spec:
      backoffLimit: 0              # the next day's run is the retry
      activeDeadlineSeconds: 43200 # every team is assessed by the model; allow for it
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: weekly
              image: image-registry.openshift-image-registry.svc:5000/alerts-bi/alerts-bi:latest
              command: ["alerts-bi", "weekly", "--registry", "/etc/alerts-bi/teams.json"]
              envFrom:
                - configMapRef: {name: alerts-bi-config}
                - secretRef: {name: alerts-bi-secrets}
              volumeMounts:
                - {name: registry, mountPath: /etc/alerts-bi}
          volumes:
            - name: registry
              configMap: {name: alerts-bi-registry}
```

`concurrencyPolicy: Forbid` stops Kubernetes starting a second Job, and the command also
holds a SQL Server application lock, so a manual `alerts-bi weekly` cannot overlap it either.
The Job **fails** whenever a week was held, failed or blocked; alert on failed Jobs, then run
`alerts-bi weekly-status` to see why. Scorecard files written under `out/weekly` are lost with
the pod; they can be re-rendered from SQL at any time with `alerts-bi report --run-id`.

Nothing here has run on a cluster.

### Superseded: one CronJob per team

Before the weekly schedule existed, a `CronJob` per team was the stopgap:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: alerts-bi-checkout-api
spec:
  schedule: "0 6 * * 1"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: run
              image: image-registry.openshift-image-registry.svc:5000/alerts-bi/alerts-bi:latest
              command: ["alerts-bi", "run", "--team", "checkout-api"]
              envFrom:
                - configMapRef: {name: alerts-bi-config}
                - secretRef: {name: alerts-bi-secrets}
```

Do not use it: it runs at "now" rather than on the Monday boundary, never publishes, and
bypasses the schedule's lock. It is kept only so an existing deployment can be recognised.

---

## 9. Verifying a deployment

In order. Each step fails loudly rather than degrading, which is the point.

```bash
oc logs job/alerts-bi-migrate           # expect: applied 001_initial_schema
oc rsh deploy/alerts-bi alerts-bi db status
```

```bash
oc port-forward svc/alerts-bi 8000:8000
```

| Check | Expect |
|---|---|
| `curl localhost:8000/healthz` | `{"ok": true, ...}` with `elasticsearch` and `sql_server` both `ok` |
| `curl localhost:8000/teams` | every team from your registry, with the operator counts you expect |
| `curl localhost:8000/openapi.json` | the generated contract |
| A run with `{"team": "<one team>", "llm": "off"}` | a scorecard, proving Elasticsearch and SQL work end to end without involving the model |
| The same run with `"llm": "live"` | `llm_assessed: true` in the JSON summary. **If this says `false`, the model is not wired up** — check `LLM_ENABLED`, and that the two guides are in the image |

That last row is the one worth being deliberate about: a model that is not reachable does not
crash the run. It records every eligible identity as `unassessed` with a reason, and the
scorecard says so honestly. Deploying without noticing is easy, which is why it is the last
check rather than an assumption.

---

## 10. What I would do before production

1. Close the authentication gap — option 1 or 2 above.
2. Decide about SQL encryption, and either wire it through or place the database so it does
   not matter.
3. Confirm the four runtime files are in the built image; a missing guide surfaces only when
   the model is enabled.
4. Run one team with `llm: off`, then with `llm: live`, and read the scorecard rather than
   only the status code.
5. Settle registry updates — ConfigMap or GitOps — and write down who is allowed to change
   ownership.
