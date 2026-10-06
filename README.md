# Alerts BI operator app

The independent operator app publishes and withdraws weekly reviews, records append-only
human decisions and renders run scorecards and team summaries from SQL Server. It uses
the shared operations package and never imports the runs pipeline or portal application.

Python 3.12+ and `uv` are required. From this repository:

```powershell
uv sync --frozen
Copy-Item .env.example .env
# Set SQL_*, a private ADMIN_SECRET (at least 32 characters), and ADMIN_REGISTRY_PATH.
uv run --frozen alerts-bi-admin serve
```

The default listener is `http://127.0.0.1:8200`. It only binds to loopback and expects
the trusted login proxy to pass `X-Forwarded-User`. For an isolated local session,
`alerts-bi-admin serve --dev-user your-name` supplies an explicit development identity.
The app refuses unsigned forms, tokens belonging to another operator, and declared
cross-site writes. Every write is attributed to its operator.

The authenticated `GET /healthz` compiles zero-row queries for the tables and columns used
by the app, scorecards and operator actions. Missing tables/columns return a generic 503.
Readiness does not fetch alert data, mutate the database or apply migrations.

Supply the same registry JSON that the runs app uses, through `ADMIN_REGISTRY_PATH`
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

The image expects an oauth-proxy sidecar sharing its loopback network and a registry mount;
it contains no credentials or migrations. A standalone clone uses the immutable wheels in
`vendor/` and the committed lockfile. Canonical design and cross-application SQL tests live
in `venaTeam/alerts-bi-design`; `docs/upstream/` contains this application's pinned snapshot.

Imported from `venaTeam/alerts-bi` at `c518eeaeb5a3ecb35b5348828300454379e7d535`.


Application code lives directly in `src/`. Local tests import `src`, while setuptools
maps that directory to the service's distinct installed Python package. The console
command and Docker listener are unchanged. `uv sync --frozen` installs the editable
mapping; `uv build` produces the independently installable wheel and source archive.
