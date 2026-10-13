# Alerts BI unified operator and analysis application

This repository owns the authenticated operator UI/API, analysis engine, SQL migrations,
weekly jobs, team registry, mock tooling, and shared/operations library sources. It replaces
the former runs service and source repository. The standalone portal remains a read-only
consumer of published SQL views and `alerts-bi-shared`.

Teams opens run history and Overview, Findings, Decisions and Activity tabs. Schedule and
Decisions have workspace-wide pages. Trigger, inspect, publish, withdraw and record human
decisions in the same application. The existing UI is preserved.

## Install and start

Python 3.12+ and Poetry 2.3.1 are required. A standalone clone includes all source and resources:

```powershell
poetry sync --no-root
poetry run python -m ensurepip --upgrade
poetry run python -m pip install --no-deps -e .
Copy-Item .env.example .env
# Configure SQL_*, ES_*, LLM_* and a private ADMIN_SECRET of at least 32 characters.
poetry run alerts-bi-admin db setup
poetry run alerts-bi-admin serve
```

`db setup` targets SQL_DATABASE unless `--database` is explicit; run it only for an authorized
database. It preserves applied revisions and rejects checksum drift. No data/schema migration
is introduced by the repository move. Schema head stays `008_measurement_basis`.

The listener binds only to loopback, default port 8200, behind the trusted login proxy's
`X-Forwarded-User`. Local-only `serve --dev-user your-name` supplies an explicit development
identity. Every HTTP route requires identity. Writes require actor-bound CSRF tokens and
same-site requests; JSON `POST /runs` gets its token from authenticated `GET /csrf`.
`GET /healthz` checks SQL, Elasticsearch and operator schema, returning a redacted 503 when
unavailable. Keep one UI worker/replica; manual analysis is synchronous and uses a process gate.

## Commands

```powershell
poetry run alerts-bi-admin run --team checkout-api --fake-llm --database alerts_bi_test
poetry run alerts-bi-admin weekly --dry-run
poetry run alerts-bi-admin weekly
poetry run alerts-bi-admin weekly-status
poetry run alerts-bi-admin db status
poetry run alerts-bi-admin registry check --registry config/teams.json
poetry run alerts-bi-admin report --run-id <id>
poetry run alerts-bi-admin publications --team <team>
poetry run alerts-bi-admin publish --run-id <id> --by <operator>
poetry run alerts-bi-admin unpublish --run-id <id> --reason <reason> --by <operator>
poetry run alerts-bi-admin decide --run-id <id> --schema v1 --application <app> --key-field <key> --finding R1 --state confirmed --note <note> --by <operator>
poetry run alerts-bi-admin decisions --team <team>
```

Manual runs cover exactly the preceding 168 hours and remain unpublished. Weekly jobs retain
the Monday UTC windows, SQL schedule lock, health gate, catch-up and publication policy.
Run exactly one weekly scheduler. Operator `--by` defaults to the OS user; web writes use the
proxy identity. Operator commands support `--database`, while the listener additionally
supports `ADMIN_DATABASE`. Registry is admin-owned `config/teams.json`; mount production values
using `ADMIN_REGISTRY_PATH` or an explicit `--registry`. The listener, registry check,
manual runs, weekly jobs and weekly status all honor that environment variable.
`ADMIN_OUT_DIR` configures HTTP output and the root for CLI reports: manual runs and
`report` write `<root>/<run id prefix>`, weekly jobs write `<root>/weekly`.
Explicit `--registry` and `--out` flags take precedence over environment settings.

### Elasticsearch sources

For an existing deployment, see the [environment changes and migration guide](docs/admin-env-changes.md).

Set `ES_V1_INDEX` and `ES_V2_INDEX` to the deployment's v1 and v2 index names, aliases,
or read patterns. They default to `appchi-v1` and `appchi-v2` for the existing local mock.
The reader and `/healthz` use the same settings; readiness requires both configured
sources to exist. Whitespace in an index setting is rejected before querying.
Settings load from `.env`, with the process environment taking precedence.
`ES_PIT_KEEP_ALIVE` controls the point-in-time lifetime for both opening and paging
the snapshot (default `2m`); use a positive duration such as `5m` or `120s`.

The mock generator, scale probe and Kibana data-view lookup use these index settings
too. The generator requires distinct concrete names and still refuses `RESET=1` on
non-loopback endpoints. Use an isolated local mock for generation/reset; source aliases
and wildcard patterns are for readers. The scale probe also honors `ADMIN_REGISTRY_PATH`.
Pure fixture helpers, fake evaluation, and CPU/memory benchmarks accept wildcard settings;
concrete-name validation happens before mock reset or bulk writes. Readiness rejects
wildcard sources with no matching indices.

## Source, libraries and builds

UI source is directly in `src/`; engine code is in `src/engine`, installed under
`alerts_bi_admin.engine`. Historical revisions use local and installed `src.db.ledger`
compatibility bridges. Never edit applied SQL/revision files. Public library namespaces remain
`alerts_bi_shared` and `alerts_bi_operations`, maintained in `packages/` as local Poetry path dependencies.
There is no runs runtime package or sibling checkout dependency.

`poetry sync --no-root` installs the locked dependencies and editable local libraries.
Existing uv-created `.venv` environments may lack pip: the `ensurepip` setup step
bootstraps it before installing the application. Keep the three setup commands together.
The application uses setuptools for its `src` to `alerts_bi_admin` mapping and
migration compatibility bridge; Poetry's root editable installer cannot preserve that
mapping. `poetry build` uses the declared setuptools backend. Docker builds and installs
the app and both library wheels for a non-editable production environment.
Use `poetry check --lock` to check the lockfile. Current service commands are in this
README; the pinned `docs/upstream/` snapshot retains its historical tooling examples.

```powershell
poetry build
poetry run python scripts/build_libraries.py
docker build -t alerts-bi-admin:local .
```

Portal pins an immutable shared wheel; copy each new versioned artifact to its vendor directory
and update its lockfile only after verifying compatibility. Never overwrite a released wheel.
An admin wheel install requires matching shared/operations wheels; the Docker build uses only
this checkout and committed lockfile. Source provenance is in `docs/engine-source-import.json`.

## Local mocks and verification

The original mock generator, scripts and hand-reviewed oracle now live here. The Compose
project defaults to `alertsbi` to reuse the existing named volumes. For an existing installation
with another project name, set COMPOSE_PROJECT_NAME to its existing name before using Compose.
Never use `down -v` for a source relocation. Configure `.env` before starting local dependencies.

```powershell
docker compose up -d
$env:STATS_ONLY = "1"
poetry run python scripts/generate_mock_alerts.py
Remove-Item Env:STATS_ONLY
poetry run ruff check .
poetry run ruff format --check .
poetry run mypy
poetry run pytest tests/unit tests/engine/unit -q
poetry run pytest tests/integration tests/engine/integration -q -rs
poetry run pytest tests/engine/acceptance -q -rs
poetry run alerts-bi-admin verify-acceptance --database alerts_bi_test
```

Integration suites require real local SQL Server and the seeded Elasticsearch mock. They reset
only disposable `alerts_bi_test`; run them serially, with no other session testing that database.
Acceptance uses the original fixed-clock fixture load; never regenerate expected-results.json.
`scripts/evaluate_llm.py`, `benchmark_streaming.py`, `es_scale_probe.py` and `create_kibana_panels.py`
retain their existing explicit options. Live model evaluation is opt-in and separate from fake tests.

The admin image provides writable `/app/out` for its non-root user. Deployment examples and
the ordered scheduler/migration transfer, rollback and eventual archival checklist are in
`docs/upstream/openshift-deployment.md` and `docs/upstream/runs-retirement.md`. PRs do not deploy
or stop services. Canonical requirements stay in alerts-bi-design; snapshots here are pinned.
