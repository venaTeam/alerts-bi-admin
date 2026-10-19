# Deploying Alerts BI admin on OpenShift

This folder holds everything needed to deploy `alerts-bi-admin` on OpenShift: the operator
console, its run worker, the SQL migrations and the single weekly scheduler. Deploy the
read-only portal afterwards with `deploy/openshift/README.md` in `alerts-bi-portal`.

The console has no login (design "Admin console without login", 2026-10-19). Anyone who has its
URL and can reach the cluster's router may use it, including Delete run, and nothing records
who did what. Share the URL only with the team that operates it.

Follow the sections in order. You supply only the values listed in [Values you supply](#3-values-you-supply);
everything else is fixed by the files here. Never edit the templates to make a deployment work.
When a result differs from the "Expect" line, stop, use [Troubleshooting](#13-troubleshooting)
and continue only once the result matches. Deploying needs authorization from the service owner;
these files do not grant it.

The requirements behind this procedure are in `docs/upstream/openshift-deployment.md` (the
pinned design). This runbook is the procedure; where the two seem to disagree, stop and ask.

| File | What it is |
|---|---|
| `admin.yaml` | Template: ServiceAccount, Deployment, Service, Route and the weekly CronJob |
| `admin-job.yaml` | Template: one `alerts-bi-admin` command run once as a Job (status checks, maintenance migration) |
| `values.env.example` | Template values: image, route, schedule, sizing |
| `config.env.example` | Non-secret settings: becomes the ConfigMap `alerts-bi-config` |
| `secrets.env.example` | Credentials: becomes the Secret `alerts-bi-secrets` |

## 1. What gets deployed

```text
operators --HTTPS--> Route alerts-bi-admin (edge TLS, no login, no client allowlist)
            --> Service alerts-bi-admin :8200
            --> admin 0.0.0.0:8200 (console)            } one pod, Deployment alerts-bi-admin,
                run-worker (manual and backfill runs)    } 1 replica, Recreate; the migrate init
                                                         } container runs first
CronJob alerts-bi-weekly --> alerts-bi-admin weekly (one scheduler for the database)
All of them --> SQL Server SQL_DATABASE (read/write), Elasticsearch (read only), on-prem model
Portal (alerts-bi-portal) --> the same SQL_DATABASE, published portal_* views only
```

| Object | Kind | Created by | Purpose |
|---|---|---|---|
| `alerts-bi-admin` | Deployment | `admin.yaml` | Console and run worker; migrations in its init container |
| `alerts-bi-admin` | Service, Route, ServiceAccount | `admin.yaml` | HTTPS entry to the console; the ServiceAccount carries the pull secret |
| `alerts-bi-weekly` | CronJob | `admin.yaml` | The only weekly scheduler; created suspended |
| `alerts-bi-config` | ConfigMap | you, from `config.env` | Non-secret settings, also read by the portal |
| `alerts-bi-secrets` | Secret | you, from `secrets.env` | SQL, Elasticsearch and model credentials |
| `alerts-bi-registry` | ConfigMap | you, from `teams.json` | The team registry, mounted at `/etc/alerts-bi/teams.json` |
| `alerts-bi-admin-secrets` | Secret | you, generated | `ADMIN_SECRET`, which signs the console's forms |
| `alerts-bi-ca-bundle` | ConfigMap | you, only for a private CA | `ca-bundle.crt`, mounted at `/etc/alerts-bi-ca` |
| `alerts-bi-pull` | Secret | you, only if the registry needs credentials | Image pull credentials |

The runs-era objects (the `alerts-bi-runs` trigger, its image and any old weekly CronJob) and
the former login proxy's objects (the `oauth-proxy` container, the Role and RoleBinding
`alerts-bi-admin-access` and the Secret `alerts-bi-admin-tls`) are replaced or removed.
[Upgrade](#7-upgrade-an-existing-installation) says how.

## 2. Before you start

You need:

- `oc` 4.14 or later, logged in (`oc whoami`) with the `edit` or `admin` role in the target
  project (removing the former login proxy's Role and RoleBinding on an upgrade needs `admin`).
  The cluster must be OpenShift 4.14 or later (`oc version` shows a Kubernetes server version of
  v1.27 or later), because the CronJob uses `timeZone`.
- A POSIX shell: bash on Linux or macOS, or Git Bash on Windows. Every command below is bash.
- `openssl` (Git for Windows includes it).
- The `alerts-bi-admin` checkout at the release commit. The templates must come from the same
  commit as the image.
- The `alerts-bi-admin` image built from that commit and pushed to a registry the cluster can
  pull from. CI builds it; in the private network the GitLab pipeline does. To build by hand:
  `podman build -t <registry>/<repository>:<commit> .` from the repository root, then
  `podman push <registry>/<repository>:<commit>`.
- The SQL login (see below), network access from the cluster to SQL Server, Elasticsearch and
  the model endpoint, and the team registry file.

SQL login requirements. The console, the run worker and the weekly job use one login
(`SQL_USER`). It must be able to:

- connect to the `master` database (the migration step checks whether `SQL_DATABASE` exists);
- create `SQL_DATABASE` if it does not exist yet (`dbcreator`), or the database owner creates
  the empty database first;
- change the schema of `SQL_DATABASE` (`db_owner`), because the init container applies migrations.

Set up your shell once per session. Every later command uses these three variables:

```bash
ADMIN_REPO=/path/to/alerts-bi-admin          # checkout at the release commit
VALUES_DIR=/path/to/private/alerts-bi-values # private directory outside every repository
NS=alerts-bi                                 # the OpenShift project to deploy into
cd "$ADMIN_REPO/deploy/openshift"
oc get project "$NS"                         # Expect: the project is listed
```

Define this helper in the same shell. It runs one `alerts-bi-admin` command as a Job with the
image, configuration and registry from your values, prints its output, and deletes the Job when
it succeeded. A failed Job is kept: inspect it with `oc describe job/<name> -n "$NS"` and delete
it before running the same name again.

```bash
admin_job() {  # usage: admin_job <job-name> <alerts-bi-admin arguments...>
  local name="$1"; shift
  oc process --local -f "$ADMIN_REPO/deploy/openshift/admin-job.yaml" \
    --param-file "$VALUES_DIR/values.env" --ignore-unknown-parameters \
    -p "JOB_NAME=$name" -p "JOB_COMMAND=$*" -o yaml | oc create -n "$NS" -f - || return 1
  oc logs -n "$NS" -f "job/$name" --pod-running-timeout=10m
  local i; for i in 1 2 3 4 5 6; do  # the Job status follows the pod by a few seconds
    [ -n "$(oc get job "$name" -n "$NS" -o jsonpath='{.status.succeeded}{.status.failed}')" ] && break
    sleep 5
  done
  if [ "$(oc get job "$name" -n "$NS" -o jsonpath='{.status.succeeded}')" = "1" ]; then
    echo "succeeded=1"; oc delete job "$name" -n "$NS" --wait=false
  else
    echo "FAILED: job/$name kept for inspection"; return 1
  fi
}
```

`admin_job` needs `alerts-bi-config`, `alerts-bi-secrets` and `alerts-bi-registry` to exist.

## 3. Values you supply

These are the only inputs. "Upgrade" says where the running deployment already holds the value.

| Value | File | Required | Where to get it |
|---|---|---|---|
| Project (`NS`) | shell | yes | The project to deploy into. Upgrade: the existing project |
| `ADMIN_IMAGE` | values.env | yes | Digest of the CI image for the release commit; see [Image references](#image-references) |
| `ADMIN_ROUTE_HOST` | values.env | no | Empty generates a host. Upgrade: `oc get route alerts-bi-admin -n "$NS" -o jsonpath='{.spec.host}'` |
| `ADMIN_ROUTE_TIMEOUT` | values.env | no | Upgrade: the Route's `haproxy.router.openshift.io/timeout` annotation, if set |
| `WEEKLY_SCHEDULE` | values.env | no | Upgrade: the schedule of the weekly CronJob in use today (section 4), `oc get cronjob <name> -n "$NS" -o jsonpath='{.spec.schedule} {.spec.timeZone}{"\n"}'`. Convert it to UTC if its time zone is not UTC |
| `UI_*`, `ANALYSIS_*` sizing | values.env | no | Upgrade: when the running containers have larger memory limits (`oc get deployment alerts-bi-admin -n "$NS" -o jsonpath='{range .spec.template.spec.containers[*]}{.name} {.resources}{"\n"}{end}'`), use those |
| `SQL_HOST`, `SQL_PORT`, `SQL_DATABASE` | config.env | yes | The database owner. Upgrade: [exported](#71-record-the-running-state) |
| `SQL_USER`, `SQL_PASSWORD` | secrets.env | yes | The database owner; see the login requirements above |
| `ES_URL`, `ES_V1_INDEX`, `ES_V2_INDEX` | config.env | yes | The Elasticsearch owner: URL and the v1/v2 source indices, aliases or patterns |
| `ES_USERNAME`, `ES_PASSWORD` | secrets.env | if ES uses basic auth | The Elasticsearch owner |
| `ES_CA_CERT` and the CA file | config.env | if ES uses a private CA | Set `ES_CA_CERT=/etc/alerts-bi-ca/ca-bundle.crt` and create [the CA bundle](#64-private-ca-bundle-only-when-needed) |
| `LLM_ENABLED` | config.env | yes | The service owner: `true` in production with the model. Upgrade: unchanged (an absent key means `false`) |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_MODEL_REVISION` | config.env | when `LLM_ENABLED=true` | The model owner. Upgrade: unchanged |
| `LLM_API_KEY` | secrets.env | if the endpoint needs a key | The model owner |
| `SSL_CERT_FILE` | config.env | if the model endpoint uses a private CA | Uncomment the line in config.env and add the CA to the bundle |
| `teams.json` | file in `$VALUES_DIR` | yes | The team registry owner. Upgrade: [exported](#71-record-the-running-state). Never the repository's `config/teams.json`, which is the local mock registry |
| Registry credentials | command line only | if the registry needs them | Registry owner; see [pull secret](#61-pull-secret-only-when-the-registry-needs-credentials) |

Keep every other value at its default.

### Prepare the value files

```bash
mkdir -p "$VALUES_DIR"
cp values.env.example "$VALUES_DIR/values.env"
cp config.env.example "$VALUES_DIR/config.env"   # upgrade: section 7.1 exports the live file instead
cp secrets.env.example "$VALUES_DIR/secrets.env" # upgrade: section 7.1 exports the live file instead
```

Fill in the REQUIRED lines. Each example file states its format rules; follow them exactly:
`config.env` and `secrets.env` take no quotes and no comments after a value.

### Image references

The image is referenced by digest (`@sha256:`), never by tag, so the pod, its init container
and the CronJob run exactly the same build.

```bash
oc image info <registry>/<repository>:<tag>
```

Expect a `Digest: sha256:<64 hex>` line. The value is `<registry>/<repository>@sha256:<64 hex>`.
If the registry needs credentials, add `--registry-config=<auth.json>`. If the output lists
several platforms, add `--filter-by-os=linux/amd64` and use that digest.

### Check the values

Rendering happens locally and changes nothing on the cluster:

```bash
oc process --local -f admin.yaml --param-file "$VALUES_DIR/values.env" --ignore-unknown-parameters -o yaml > "$VALUES_DIR/admin.rendered.yaml"
grep 'image:' "$VALUES_DIR/admin.rendered.yaml" | grep -vc '@sha256:'
```

Expect: `oc process` prints nothing, and `grep` prints `0` (every image is pinned by digest).
`parameter X is required and must be specified` means that line of `values.env` is empty.

## 4. Fresh install or upgrade?

```bash
oc get deployment,cronjob,configmap,secret,route,service -n "$NS" -o name
oc get deployment -n "$NS" -o custom-columns='NAME:.metadata.name,REPLICAS:.spec.replicas,IMAGES:.spec.template.spec.containers[*].image'
oc get cronjob -n "$NS" -o custom-columns='NAME:.metadata.name,SUSPEND:.spec.suspend,IMAGE:.spec.jobTemplate.spec.template.spec.containers[0].image,COMMAND:.spec.jobTemplate.spec.template.spec.containers[0].command,ARGS:.spec.jobTemplate.spec.template.spec.containers[0].args'
```

- Neither `deployment.apps/alerts-bi-admin` nor any CronJob running a `weekly` command exists:
  this is a fresh install. Continue with section 6.
- Otherwise it is an upgrade. Continue with section 7. Every CronJob whose command or args run `weekly`
  (`alerts-bi-admin weekly`, `alerts-bi-runs weekly` or `alerts-bi weekly`) is a weekly
  scheduler; note their names. Also note every Deployment other than `alerts-bi-admin` whose
  image or command is `alerts-bi-runs` or `alerts-bi` (the runs-era trigger): it is an old writer.

## 5. Migrations: what the init container does

The `migrate` init container runs `alerts-bi-admin db setup` before the console and worker
start. It creates `SQL_DATABASE` if absent, applies only the missing migrations and refuses
checksum drift. While it fails, the other containers never start and OpenShift retries it.
It has no time limit on purpose: never add one, and never delete the pod while it runs. If it is
killed, the running migration rolls back and the next start repeats it from the beginning.

Before every upgrade, list the pending migrations with the new image (section 7.3). A pending
migration means a [migration gate](#74-migration-gate). Migrations are roll-forward only: once one
is applied, an older image stops in its init container with
`Can't locate revision identified by '<revision>'`, and only a database restore goes back.

`018_erase_operator_names` permanently blanks every stored operator name: publishers,
withdrawers, decision makers, run and backfill requesters, and cancellers named in run messages.
The scheduler's own `weekly-schedule` marker stays. Only the pre-018 backup from the migration
gate holds those names afterwards.

## 6. Fresh install

### 6.1 Pull secret (only when the registry needs credentials)

Skip this if the cluster can already pull the images (for example a cluster-wide pull secret).

```bash
oc create secret docker-registry alerts-bi-pull -n "$NS" --docker-server=<registry> --docker-username=<user> --docker-password=<password>
```

The name must equal `IMAGE_PULL_SECRET` in `values.env`. Without this Secret, pods pull without
credentials and show a harmless `FailedToRetrieveImagePullSecret` warning event.

### 6.2 Configuration and credentials

```bash
oc create configmap alerts-bi-config -n "$NS" --from-env-file="$VALUES_DIR/config.env" --dry-run=client -o yaml | oc apply -n "$NS" -f -
oc create secret generic alerts-bi-secrets -n "$NS" --from-env-file="$VALUES_DIR/secrets.env" --dry-run=client -o yaml | oc apply -n "$NS" -f -
rm "$VALUES_DIR/secrets.env"
```

Expect `configmap/alerts-bi-config created` and `secret/alerts-bi-secrets created`. Then
generate the console's own secrets. They never leave the cluster:

```bash
oc create secret generic alerts-bi-admin-secrets -n "$NS" --from-literal=ADMIN_SECRET="$(openssl rand -hex 32)"
```

### 6.3 Team registry

```bash
oc create configmap alerts-bi-registry -n "$NS" --from-file=teams.json="$VALUES_DIR/teams.json" --dry-run=client -o yaml | oc apply -n "$NS" -f -
admin_job alerts-bi-registry-check registry check
```

Expect `registry <version> is valid: <n> teams` followed by one line per team, and
`succeeded=1`. On `registry is invalid`, fix `teams.json` and repeat both commands.

### 6.4 Private CA bundle (only when needed)

Needed when Elasticsearch or the model endpoint presents a certificate from a private CA.
Put every CA certificate needed, PEM encoded, in one file:

```bash
oc create configmap alerts-bi-ca-bundle -n "$NS" --from-file=ca-bundle.crt="$VALUES_DIR/ca-bundle.crt" --dry-run=client -o yaml | oc apply -n "$NS" -f -
```

Then make sure `config.env` has `ES_CA_CERT=/etc/alerts-bi-ca/ca-bundle.crt` (Elasticsearch)
and/or the uncommented `SSL_CERT_FILE=/etc/alerts-bi-ca/ca-bundle.crt` line (model endpoint;
the file then replaces the default trust store for model calls), and re-run the ConfigMap
command from 6.2.

### 6.5 Check the database connection

```bash
admin_job alerts-bi-db-status db status
```

Expect `database: <SQL_DATABASE>`, then every migration listed as `pending` and `succeeded=1`.
An error naming `Cannot open database` means the database does not exist yet; the init
container creates it if the login has `dbcreator`. Any other error (login failed, timeout,
host not found) must be fixed before continuing.

### 6.6 Deploy

The weekly CronJob is created suspended (`WEEKLY_SUSPEND=true`).

```bash
oc process --local -f admin.yaml --param-file "$VALUES_DIR/values.env" --ignore-unknown-parameters -o yaml > "$VALUES_DIR/admin.rendered.yaml"
oc apply -n "$NS" --dry-run=server -f "$VALUES_DIR/admin.rendered.yaml"
oc apply -n "$NS" -f "$VALUES_DIR/admin.rendered.yaml"
oc rollout status deployment/alerts-bi-admin -n "$NS" --timeout=60m
```

Expect the dry run to list five objects ending in `(server dry run)`, the apply to list them as
`created`, and `deployment "alerts-bi-admin" successfully rolled out`. Then continue with
section 8 (verify) and section 9 (enable the weekly scheduler).

## 7. Upgrade an existing installation

### 7.1 Record the running state

Keep these files private; `previous/` is what a rollback needs.

```bash
mkdir -p "$VALUES_DIR/previous"
oc get deployment,cronjob,service,route,serviceaccount,role,rolebinding -n "$NS" -o yaml > "$VALUES_DIR/previous/objects.yaml"
oc get configmap alerts-bi-config -n "$NS" -o go-template='{{range $k, $v := .data}}{{$k}}={{$v}}{{"\n"}}{{end}}' > "$VALUES_DIR/config.env"
oc get secret alerts-bi-secrets -n "$NS" -o go-template='{{range $k, $v := .data}}{{$k}}={{$v | base64decode}}{{"\n"}}{{end}}' > "$VALUES_DIR/secrets.env"
oc extract configmap/alerts-bi-registry -n "$NS" --keys=teams.json --to="$VALUES_DIR" --confirm
cp "$VALUES_DIR/config.env" "$VALUES_DIR/teams.json" "$VALUES_DIR/previous/"
oc get deployment alerts-bi-admin -n "$NS" -o jsonpath='{range .spec.template.spec.containers[*]}{.name}{"  "}{.image}{"\n"}{end}' | tee "$VALUES_DIR/previous/images.txt"
```

If a ConfigMap or Secret above does not exist under that name, find the one the running pod
uses (`oc get deployment alerts-bi-admin -n "$NS" -o yaml`, look at `envFrom` and `volumes`)
and export that instead. The new templates use the names in section 1: once the files below are
complete, create those objects at once with the configuration commands of 7.5 (not its scheduler
step). New names do not affect the running pods, and `admin_job` in 7.3 needs them.

Fill `values.env` from section 3 using the live values. Then compare the exported
configuration with the expected keys:

```bash
diff <(grep -oE '^[A-Z0-9_]+' config.env.example | sort) <(grep -oE '^[A-Z0-9_]+' "$VALUES_DIR/config.env" | sort)
```

- `<` lines are keys to add to `config.env` with the value from `config.env.example`.
- `>` lines are keys the live ConfigMap has but the example does not. Delete these from
  `config.env`: `ADMIN_OUT_DIR`, `API_OUT_DIR`, `API_HOST`, `API_PORT`, `API_REGISTRY_PATH`,
  `API_DATABASE`, `ADMIN_HOST`, `ADMIN_PORT`, `ADMIN_REGISTRY_PATH`, `PORTAL_DATABASE`,
  `PORTAL_SQL_USER`, `PORTAL_SQL_PASSWORD`. Move `SQL_USER`, `SQL_PASSWORD`, `ES_USERNAME`,
  `ES_PASSWORD`, `LLM_API_KEY` or `ADMIN_SECRET` to `secrets.env` (ADMIN_SECRET belongs in
  `alerts-bi-admin-secrets`). Keep any other key and report it.
- `ADMIN_DATABASE`: delete it when it is empty or equals `SQL_DATABASE`. When it names another
  database, stop: the weekly job and the portal always use `SQL_DATABASE`, so the database
  owner must say which database is the real one before you continue.

Compare the exported credentials the same way:

```bash
diff <(grep -oE '^[A-Z0-9_]+' secrets.env.example | sort) <(grep -oE '^[A-Z0-9_]+' "$VALUES_DIR/secrets.env" | sort)
```

Add each `<` key empty. Treat each `>` key by the rules above: `ADMIN_DATABASE` in the Secret
follows the `ADMIN_DATABASE` rule, a non-secret setting moves to `config.env`, the obsolete keys
are deleted, and any other key is kept and reported.

Then check whether the running deployment still has the former login proxy:

```bash
oc get deployment alerts-bi-admin -n "$NS" -o jsonpath='{range .spec.template.spec.containers[*]}{.name}{"\n"}{end}'
oc get role/alerts-bi-admin-access rolebinding/alerts-bi-admin-access secret/alerts-bi-admin-tls -n "$NS" --ignore-not-found -o name
```

If the first command lists `oauth-proxy` or the second lists anything, section 7.6 removes them
first ("Remove the login proxy"). Then check that `alerts-bi-admin-secrets` exists:

```bash
oc get secret alerts-bi-admin-secrets -n "$NS" -o go-template='{{range $k, $v := .data}}{{$k}}{{"\n"}}{{end}}'
```

Expect `ADMIN_SECRET` (a leftover `COOKIE_SECRET` is removed in 7.6). Keep the existing Secret.
If it is missing, create it as in section 6.2. If only `ADMIN_SECRET` is missing, add it:
`oc set data secret/alerts-bi-admin-secrets -n "$NS" ADMIN_SECRET="$(openssl rand -hex 32)"`.

### 7.2 Announce the window and drain

The upgrade replaces the pod: the console is unavailable until the new pod is ready, and an
analysis still running is interrupted (it then shows as failed and must be started again).
Tell the operators, ask them not to start runs, and check for unfinished runs:

```bash
oc exec -n "$NS" deploy/alerts-bi-admin -c run-worker -- python -c '
from alerts_bi_shared.config.sql import load_sql_config
from alerts_bi_shared.db.connection import connect
with connect(load_sql_config()) as db:
    print(db.query("SELECT status, COUNT(*) AS jobs FROM run_jobs GROUP BY status"))
'
```

Expect no `queued`, `pending` or `running` rows. Wait and repeat while there are any. If the
container `run-worker` does not exist or `run_jobs` is an invalid object name, the running
version has no run queue: skip this check.

### 7.3 List pending migrations with the new image

```bash
admin_job alerts-bi-db-status db status
```

Expect `succeeded=1`, a `revision:` line naming a migration listed below it, and every line
`applied` or `pending`.

- `APPLIED BUT FILE CHANGED`: stop. The database and the image disagree; never stamp or edit.
- `revision:` names a migration that is not listed: the database is newer than this image; stop.
- No `pending` line: no migration gate. Continue with 7.5.
- Any `pending` line: continue with 7.4.

### 7.4 Migration gate

Pending migrations can lock tables for a long time and cannot be undone except by restoring a
backup. 015, 016 and 017 rebuild the primary keys and indexes of the largest tables; while they
run, portal pages and every query on those tables wait. 018 erases stored operator names for
good (section 5). Before applying any pending migration,
the database owner must confirm all of the following to you in writing. You cannot do them yourself:

1. They will take a database backup in this window, after section 7.5 has stopped every
   writer and before the first pending migration, and confirm that it restores.
2. The window is sized from a rehearsal on a restored copy of comparable size, including index
   rebuilds and transaction-log space (015 and 016 each rewrite `alert_findings` and
   `llm_verdicts`; 017 changes eight team tables).
3. The maintenance window lies outside `WEEKLY_SCHEDULE`, and portal readers have been told
   that pages may wait.
4. How to run the migrations: `init container` (the default) or `maintenance job` (the database
   owner wants the migration run and watched on its own before the console starts).

Without all four, stop. Then record which migrations were pending. Section 7.5 ends with the
backup; if the answer to 4 is `maintenance job`, follow
[Maintenance migration](#maintenance-migration) after 7.5 and before 7.6.

### 7.5 Stop the old scheduler and update configuration

Suspend every weekly scheduler noted in section 4 and wait until none is running:

```bash
oc patch cronjob <name> -n "$NS" --type merge -p '{"spec":{"suspend":true}}'
oc get cronjob <name> -n "$NS" -o jsonpath='{.status.active}{"\n"}'
```

Repeat the second command until it prints an empty line. Stop every old writer noted in
section 4 (the runs-era trigger) and keep it stopped: no image older than this release may write
to the migrated database. Its JSON callers move to the console's authenticated `POST /run-jobs`.

```bash
oc scale deployment/<old-trigger> -n "$NS" --replicas=0
```

Then install the configuration (skip these three commands if 7.1 already created the objects
and the files have not changed since):

```bash
oc create configmap alerts-bi-config -n "$NS" --from-env-file="$VALUES_DIR/config.env" --dry-run=client -o yaml | oc apply -n "$NS" -f -
oc create secret generic alerts-bi-secrets -n "$NS" --from-env-file="$VALUES_DIR/secrets.env" --dry-run=client -o yaml | oc apply -n "$NS" -f -
oc create configmap alerts-bi-registry -n "$NS" --from-file=teams.json="$VALUES_DIR/teams.json" --dry-run=client -o yaml | oc apply -n "$NS" -f -
```

A warning about the missing `last-applied-configuration` annotation is expected the first time.
`oc apply` does not delete keys it did not create, so check that the live ConfigMap and Secret
have exactly the keys of your files:

```bash
diff <(grep -oE '^[A-Z0-9_]+' "$VALUES_DIR/config.env" | sort) <(oc get configmap alerts-bi-config -n "$NS" -o go-template='{{range $k, $v := .data}}{{$k}}{{"\n"}}{{end}}' | sort)
diff <(grep -oE '^[A-Z0-9_]+' "$VALUES_DIR/secrets.env" | sort) <(oc get secret alerts-bi-secrets -n "$NS" -o go-template='{{range $k, $v := .data}}{{$k}}{{"\n"}}{{end}}' | sort)
```

Expect no output from either. Remove each `>` key: `oc set data configmap/alerts-bi-config -n "$NS" <KEY>-`
for the first, `oc set data secret/alerts-bi-secrets -n "$NS" <KEY>-` for the second. A key left in
the Secret overrides the ConfigMap (the Secret is loaded last), so a stale `SQL_ENCRYPT` there would
silently win. When both diffs are empty, delete the credentials file:

```bash
rm "$VALUES_DIR/secrets.env"
```

Create the CA bundle (6.4) if `config.env` refers to `/etc/alerts-bi-ca/`, and the pull secret
(6.1) if needed.

If 7.4 applies (pending migrations), stop the console as well, so nothing writes after the backup:

```bash
oc scale deployment/alerts-bi-admin -n "$NS" --replicas=0
oc get pods -n "$NS" -l app=alerts-bi-admin
```

When no pod is listed, every writer has stopped. Ask the database owner to take the backup now
and to confirm in writing that it restores. Continue only after that confirmation.

### 7.6 Deploy

**Remove the login proxy** (only when 7.1 found it). `oc apply` would leave the old proxy
container, Service port and Route allowlist in place, so delete the login-era objects first.
This stops the old pod, as Recreate would. First make sure `ADMIN_ROUTE_HOST` in `values.env`
is the host 7.1 printed, because the Route is recreated:

```bash
oc delete deployment/alerts-bi-admin service/alerts-bi-admin route/alerts-bi-admin -n "$NS" --wait=true
oc delete role/alerts-bi-admin-access rolebinding/alerts-bi-admin-access secret/alerts-bi-admin-tls -n "$NS" --ignore-not-found
oc annotate serviceaccount/alerts-bi-admin -n "$NS" serviceaccounts.openshift.io/oauth-redirectreference.admin-
```

If 7.1 listed `COOKIE_SECRET`, remove it too:
`oc set data secret/alerts-bi-admin-secrets -n "$NS" COOKIE_SECRET-`.

Then deploy. The weekly CronJob stays suspended (`WEEKLY_SUSPEND=true`) until section 9.

```bash
oc process --local -f admin.yaml --param-file "$VALUES_DIR/values.env" --ignore-unknown-parameters -o yaml > "$VALUES_DIR/admin.rendered.yaml"
oc apply -n "$NS" --dry-run=server -f "$VALUES_DIR/admin.rendered.yaml"
oc apply -n "$NS" -f "$VALUES_DIR/admin.rendered.yaml"
oc rollout status deployment/alerts-bi-admin -n "$NS" --timeout=24h
```

Expect `created`, `configured` or `unchanged` for each object and `successfully rolled out`.
The apply sets the replicas back to 1. Recreate stops the old pod before the new pod's init
container starts; never delete pods by hand to hurry it. While migrations run, `oc get pods -n "$NS" -l app=alerts-bi-admin` shows `Init:0/1`
and `oc logs -n "$NS" -l app=alerts-bi-admin -c migrate -f` shows progress. Do not interrupt it.
If the CronJob `alerts-bi-weekly` already existed, the apply has switched it to this image and
command, still suspended.

### 7.7 Verify, then retire the old objects

Run section 8. Then run section 9 to enable `alerts-bi-weekly`. Old weekly CronJobs under other
names stay suspended. After one scheduled cycle has succeeded, and only with the service owner's
authorization, delete the obsolete runs-era objects (the old trigger Deployment, Service and
Route, old weekly CronJobs and old migration Jobs). Keep the previous images, `previous/` and
all data for the agreed rollback period.

## 8. Verify

```bash
oc logs -n "$NS" deploy/alerts-bi-admin -c migrate
```

Expect one `applied <revision>` line per pending migration, or `database is up to date`.

```bash
ROUTE_HOST=$(oc get route alerts-bi-admin -n "$NS" -o jsonpath='{.spec.host}')
curl -sk -w '\n%{http_code}\n' "https://$ROUTE_HOST/healthz"
```

Run the `curl` from a machine that can reach the router. Expect JSON starting `{"ok":true`,
whose `checks` show `elasticsearch` and `sql_server` each with `"ok":true`, then `200`. A `503`
has `"ok":false` and marks the failing check; a failing schema check appears as an extra
`operator_schema` entry (Troubleshooting). There is no sign-in step: the console answers anyone
with the URL.

```bash
oc exec -n "$NS" deploy/alerts-bi-admin -c admin -- alerts-bi-admin db status
oc exec -n "$NS" deploy/alerts-bi-admin -c admin -- alerts-bi-admin registry check
oc exec -n "$NS" deploy/alerts-bi-admin -c admin -- alerts-bi-admin weekly --dry-run
oc exec -n "$NS" deploy/alerts-bi-admin -c admin -- alerts-bi-admin weekly-status
```

Expect every migration `applied` (the last one `018_erase_operator_names`), the registry valid
with the expected teams, the due weeks listed without anything being run, and each enrolled
team's latest published week.

Ask an operator to check in a browser: the console opens without signing in and shows no
"signed in" name; the Teams page lists the registry's teams; a team week opens; a test run
completes, for a team and week the service owner approves for testing; publishing, a decision
and a withdrawal work and name nobody; the three CSV downloads open. After the portal is
deployed (its runbook), check that it shows the published week.

## 9. Enable the weekly scheduler

Only after section 8 passed. Make sure no other scheduler is active:

```bash
oc get cronjob -n "$NS" -o custom-columns='NAME:.metadata.name,SUSPEND:.spec.suspend,ACTIVE:.status.active'
```

Expect every CronJob that runs `weekly` to show `true` and no active Job. Then set
`WEEKLY_SUSPEND=false` in `$VALUES_DIR/values.env` and apply:

```bash
oc process --local -f admin.yaml --param-file "$VALUES_DIR/values.env" --ignore-unknown-parameters -o yaml > "$VALUES_DIR/admin.rendered.yaml"
oc apply -n "$NS" -f "$VALUES_DIR/admin.rendered.yaml"
oc get cronjob alerts-bi-weekly -n "$NS" -o jsonpath='{.spec.suspend}{"\n"}'
```

Expect `false`. Keep `WEEKLY_SUSPEND=false` in `values.env` from now on, or the next apply
suspends the scheduler again. To check the job at once instead of waiting for the schedule,
outside the hours of manual runs for the same teams:

```bash
oc create job -n "$NS" --from=cronjob/alerts-bi-weekly alerts-bi-weekly-check
oc logs -n "$NS" -f job/alerts-bi-weekly-check --pod-running-timeout=10m
```

The Job succeeds when every due week ran and published; held or failed weeks make it fail on
purpose. Inspect `alerts-bi-admin weekly-status` (section 8) after each run. Delete the check
Job afterwards: `oc delete job alerts-bi-weekly-check -n "$NS"`.

## 10. Day-2 operations

**New release.** Build the image from the new commit, set `ADMIN_IMAGE`, check out the templates
at that commit and compare the new `*.env.example` files with yours (the `diff` of 7.1). Run
7.2, 7.3 and 7.4. Then stop the scheduler, because Recreate does not stop a running weekly Job
and migrations do not wait for it: set `WEEKLY_SUSPEND=true` in `values.env`, suspend
`alerts-bi-weekly` and wait until it has no active Job (the first block of 7.5, for
`alerts-bi-weekly`). When 7.4 applies, also stop the console and have the backup taken as at the
end of 7.5. Then run 7.6 (it renders the CronJob still suspended, on the new image) and section 8,
and only then section 9, which sets `WEEKLY_SUSPEND=false` again.

**Configuration or credential change.** Edit `config.env`, or re-create `secrets.env` with the
export command from 7.1 and edit it, run the matching command from 7.5 (it deletes `secrets.env`
again), then restart the pod. The CronJob reads the new values at its next run.

```bash
oc rollout restart deployment/alerts-bi-admin -n "$NS"
oc rollout status deployment/alerts-bi-admin -n "$NS" --timeout=60m
```

Restart the portal too after an SQL change (portal runbook).

**Registry change.** Increment `registry_version` in `teams.json`. Validate it with the running
image before installing it, then install and restart:

```bash
oc exec -i -n "$NS" deploy/alerts-bi-admin -c admin -- sh -c 'cat > /tmp/teams.json && alerts-bi-admin registry check --registry /tmp/teams.json' < "$VALUES_DIR/teams.json"
oc create configmap alerts-bi-registry -n "$NS" --from-file=teams.json="$VALUES_DIR/teams.json" --dry-run=client -o yaml | oc apply -n "$NS" -f -
oc rollout restart deployment/alerts-bi-admin -n "$NS"
```

**Rotate the form secret.** Recreate `alerts-bi-admin-secrets` (6.2, with `oc delete secret
alerts-bi-admin-secrets -n "$NS"` first) and restart. Forms left open fail once and must be reloaded.

**Run a command.** With the console running, use `oc exec -n "$NS" deploy/alerts-bi-admin -c admin --
alerts-bi-admin <command>`. Otherwise use `admin_job <job-name> <command>`. Never run `db reset-test`,
`db migrate` or `db setup` against the deployment except as described in this file.

### Maintenance migration

Only when the migration gate chose `maintenance job`, or the database owner asks for it. First
stop every writer (on a first upgrade 7.5 already did; `alerts-bi-weekly` may not exist yet):

```bash
oc patch cronjob alerts-bi-weekly -n "$NS" --type merge -p '{"spec":{"suspend":true}}'
oc scale deployment/alerts-bi-admin -n "$NS" --replicas=0
```

Then check, and repeat the checks until both print nothing:

```bash
oc get pods -n "$NS" -l app=alerts-bi-admin -o name
oc get cronjob -n "$NS" -o jsonpath='{range .items[*]}{.metadata.name}{" active="}{.status.active}{"\n"}{end}' | grep 'active=\['
```

Then run the migration:

```bash
admin_job alerts-bi-migrate db setup
```

Expect `applied <revision>` lines and `succeeded=1`. If it fails, do not start the console; fix
the cause, delete the kept Job and run `admin_job` again. Then continue with 7.6, whose apply
sets the replicas back to 1 (outside an upgrade: `oc scale deployment/alerts-bi-admin -n "$NS"
--replicas=1` and, if you suspended it here, re-apply `admin.yaml` to restore the scheduler).

## 11. Turn on SQL encryption

`SQL_ENCRYPT=true` makes every SQL connection of the console, its jobs and the portal require
TLS. The server certificate is not validated: TLS stops passive reading, not an active
interceptor. Do it as its own change, after a normal release:

1. Confirm `SQL_ENCRYPT` is `true` or `false` (any other spelling stops startup) and
   `SQL_TRUST_SERVER_CERTIFICATE` is not `false`, in `alerts-bi-config` and any portal-only Secret.
2. Confirm with the database owner that SQL Server accepts encrypted connections from the
   cluster network. A server that does not stops the init container, console, worker, jobs and portal.
3. Set `SQL_ENCRYPT=true` in `config.env`, apply it (7.5) and restart the console and the portal.
   The init container is the first encrypted connection.
4. Verify section 8 and the portal, and run one weekly Job by hand (section 9).

Rollback: set `SQL_ENCRYPT=false`, apply and restart.

## 12. Rollback

Rollback means redeploying the previous release with its own files and image.

- **No migration was applied by this release** (7.3 listed none pending): check out the previous
  release commit, put the previous digests from `previous/images.txt` in `values.env` and deploy
  with that commit's procedure. If the previous commit has no `deploy/openshift/`, re-apply its
  manifests with the previous values, or `previous/objects.yaml` after removing `status`,
  `metadata.resourceVersion`, `metadata.uid` and `metadata.managedFields`.
- **A migration was applied**: the previous image cannot start against the migrated database.
  Either deploy a newer image that knows the applied revisions, or have the database owner
  restore the backup from the migration gate together with the previous image, files and
  registry. A restore loses every write since the backup; it needs the service owner's
  explicit authorization and stopped writers.
- **Queued runs** (from the backfill feature) must be finished or cancelled before rolling back
  to an image that predates migration 014; `docs/upstream/manual-worker-runbook.md` has the steps.
- **Scheduler**: suspend `alerts-bi-weekly` before re-enabling any old scheduler.

Never downgrade, stamp, edit or reset migrations, and never delete data to make a rollback fit.

## 13. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `oc process`: `parameter X is required and must be specified` | Fill X in `values.env` |
| Pod `Init:0/1` for a long time | Migrations are running; follow `oc logs -n "$NS" -l app=alerts-bi-admin -c migrate -f`. Never interrupt it |
| `Init:Error` or `Init:CrashLoopBackOff` | `oc logs ... -c migrate`. `Login failed` or `Cannot open database`: credentials or permissions (section 2). Timeout: network to SQL Server |
| migrate log `Can't locate revision identified by '...'` | The image is older than the database. Deploy an image that has that migration; never stamp |
| `CreateContainerConfigError` | A ConfigMap, Secret or key is missing; `oc describe pod -n "$NS" -l app=alerts-bi-admin` names it |
| `ImagePullBackOff` | Wrong digest or missing credentials: check `oc describe pod`, the digest, and 6.1 |
| Event `FailedToRetrieveImagePullSecret` | Harmless when the registry needs no credentials (6.1) |
| admin container exits: `ADMIN_SECRET must be at least 32 characters` | Recreate `alerts-bi-admin-secrets` (6.2) |
| Container exits: `env SQL_ENCRYPT must be one of ...` or `SQL_TRUST_SERVER_CERTIFICATE=false is not supported` | Fix `config.env` (section 11) |
| Container exits naming `ES_V1_INDEX`, `ES_PIT_KEEP_ALIVE`, `LLM_MAX_BATCH_SIZE` or another setting | The value is invalid; `config.env.example` gives the accepted form |
| `/healthz` 503, `elasticsearch` not ok | `ES_URL`, credentials, CA (`ES_CA_CERT`), or an index setting matches nothing |
| `/healthz` 503, `sql_server` or `operator_schema` not ok | SQL unreachable, or the schema is behind: check the migrate log and `db status` |
| Browser: `Application is not available` | No ready pod: `oc get pods -n "$NS" -l app=alerts-bi-admin` and the rows above |
| Browser shows an OpenShift login page or a client refusal | The former login proxy or Route allowlist is still in place: run "Remove the login proxy" in 7.6 and re-apply |
| Route shows `HostAlreadyClaimed` | `ADMIN_ROUTE_HOST` is used by another Route; use the existing host or another name |
| Apply or dry run fails on the Route's `spec.host` (immutable, or `routes/custom-host` forbidden) | `ADMIN_ROUTE_HOST` differs from the live Route's host, and changing an existing Route's host needs a right the `edit`/`admin` roles lack. Use the live host; to move to another host, `oc delete route/alerts-bi-admin -n "$NS"` within the window and re-apply (creating a Route with a host needs only the default rights) |
| Apply fails: `spec.selector: ... field is immutable` | The existing `alerts-bi-admin` Deployment uses other labels. Within the window, `oc delete deployment alerts-bi-admin -n "$NS" --wait=true` (it stops the old pod, as Recreate would) and repeat the apply |
| Model calls fail with certificate errors | Add the model's CA to `alerts-bi-ca-bundle` and set `SSL_CERT_FILE` (6.4) |
| Pods rejected: `must specify limits.cpu` | The project's quota needs CPU limits. Stop and ask the project owner for a LimitRange with default CPU limits |
| `admin_job` prints `FAILED` during a long migration | The log stream ended before the Job did. `oc get job <name> -n "$NS"` shows whether it still runs; follow it again with `oc logs -n "$NS" -f job/<name>` |
| Weekly Job failed | `oc logs job/<name> -n "$NS"` and `weekly-status`. Held or failed weeks fail the Job on purpose; the next run retries due weeks |

## 14. Rules that always apply

- One `alerts-bi-admin` replica with the Recreate strategy. Never scale it above 1 or change the strategy.
- One weekly scheduler per database: `alerts-bi-weekly`, enabled only by section 9.
- One `ADMIN_IMAGE` digest for the init container, console, worker, CronJob and admin Jobs.
- The console has no login: anyone with the Route URL who can reach the router may run, publish,
  withdraw, decide and permanently delete runs. Share the URL only with the operating team, and
  expose it through nothing but its Route.
- `ADMIN_DATABASE` stays unset: the weekly job and the portal always use `SQL_DATABASE`.
- Migrations only through the init container or the maintenance Job above; never with the console
  running, never with a deadline, never stamped, downgraded, edited or reset.
- Deploy admin before the portal whenever a release contains migrations.
- Secrets live only in the cluster: never commit filled value files or paste them anywhere.
