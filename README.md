# Alerts BI operator app

The unified operator console lives in this repository. Teams opens a team's run history
and its Overview, Findings, Decisions and Activity tabs. Schedule and Decisions also have
workspace-wide pages. Trigger analysis, inspect stored evidence, publish or withdraw a
review, and record append-only human decisions from the same authenticated application.

The UI source is in `src/`. Analysis runs in-process through the pinned execution runtime
in `vendor/`; no sibling checkout or separate trigger web service is required. The runtime
is exported from the tracked runs revision recorded in `vendor/runtime-source.json`.
Shared/operations wheels remain immutable dependencies. Portal stays a separate reader
service; runs remains the migration and weekly-job owner.

Python 3.12+ and `uv` are required. From this repository:

```powershell
uv sync --frozen
Copy-Item .env.example .env
# Set SQL_*, ES_*, LLM_*, a private ADMIN_SECRET (32+ characters), and registry path.
uv run --frozen alerts-bi-admin serve
```

The default listener is `http://127.0.0.1:8200`. It only binds to loopback and expects
the trusted login proxy to pass `X-Forwarded-User`. For an isolated local session,
`alerts-bi-admin serve --dev-user your-name` supplies an explicit development identity.
The app refuses unsigned forms, tokens belonging to another operator, and declared
cross-site writes. Every write is attributed to its operator.

The authenticated `GET /healthz` checks Elasticsearch, SQL and the operator schema.
Missing dependencies return JSON with `ok=false` and HTTP 503. All API routes require
operator identity. JSON `POST /runs` also requires the `X-CSRF-Token` returned by `GET /csrf`.
Browser actions use signed, same-site forms. A manual run covers exactly the preceding
168 hours and stays unpublished after completion. The UI and JSON API share one process
execution gate; deploy one application worker and one replica.

The bundled `config/teams.json` is an exported runs artifact, not an independent registry.
Supply your deployment registry through `ADMIN_REGISTRY_PATH`
or `serve --registry <path>`. It is runtime configuration, not a dependency on a sibling
checkout. The schema validator is packaged in the operations wheel. Database migrations
and the original mock stack belong to `venaTeam/alerts-bi-runs` and must be applied first.

Existing operator commands are available under the new executable:

```powershell
uv run --frozen alerts-bi-admin registry check --registry <path>
uv run --frozen alerts-bi-admin publications --team <team>
uv run --frozen alerts-bi-admin publish --run-id <id> --by <operator>
uv run --frozen alerts-bi-admin unpublish --run-id <id> --reason <reason> --by <operator>
uv run --frozen alerts-bi-admin decide --run-id <id> --schema v1 --application <app> --key-field <key> --finding R1 --state confirmed --note <note> --by <operator>
uv run --frozen alerts-bi-admin decisions --team <team>
```

`--by` defaults to the current OS user, preserving the previous CLI behavior. Publication
overlaps and gaps, explicit replacement and immutable decision history use the same
operations implementation as scheduled runs. The listener's `ADMIN_DATABASE`/`--database`
override remains supported; operator commands default to `SQL_DATABASE`.

```powershell
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy
uv run --frozen pytest -q
uv build
docker build -t alerts-bi-admin:local .
```

The image expects an oauth-proxy sidecar sharing its loopback network, a registry mount,
and the SQL/ES/model settings. `ADMIN_OUT_DIR` must be writable; the image provides
`/app/out` for its non-root user. Apply migrations through the runs deployment before use. A standalone clone uses the immutable wheels in
`vendor/` and the committed lockfile. Canonical design and cross-application SQL tests live
in `venaTeam/alerts-bi-design`; `docs/upstream/` contains this application's pinned snapshot.

Imported from `venaTeam/alerts-bi` at `c518eeaeb5a3ecb35b5348828300454379e7d535`.


Application code lives directly in `src/`. Local tests import `src`, while setuptools
maps that directory to the service's distinct installed Python package. The console
command and Docker listener are unchanged. `uv sync --frozen` installs the editable
mapping; `uv build` produces the independently installable wheel and source archive.


For the execution artifact's provenance and rebuild procedure, see `vendor/README.md`.
The integration suite resets only the configured disposable `alerts_bi_test` database and
uses the existing Elasticsearch mock. To run it, provide the corresponding SQL and ES
settings in your environment; unit checks do not require running services.
