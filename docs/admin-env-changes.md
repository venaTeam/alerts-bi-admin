# Admin service environment configuration changes

This guide covers [PR #3](https://github.com/venaTeam/alerts-bi-admin/pull/3).
It explains the configuration changes and what to add to an existing deployment.

## Environment variables to add

Add these three variables to the admin service configuration and to any separate
process running `alerts-bi-admin run` or `alerts-bi-admin weekly`:

```dotenv
ES_V1_INDEX=appchi-v1
ES_V2_INDEX=appchi-v2
ES_PIT_KEEP_ALIVE=2m
```

These values preserve the previous behavior. Replace the index values with your
deployment's actual v1 and v2 sources if they differ from the local mock names.
All three variables are optional: unset or empty values use the defaults below.

| Variable | Default | Purpose and accepted values |
| --- | --- | --- |
| `ES_V1_INDEX` | `appchi-v1` | Source for alerts with the v1 schema. Accepts an index name, alias, or read pattern, such as `legacy-alerts-*`. |
| `ES_V2_INDEX` | `appchi-v2` | Source for alerts with the v2 schema. Accepts an index name, alias, or read pattern, such as `current-alerts-*`. |
| `ES_PIT_KEEP_ALIVE` | `2m` | Lifetime of the Elasticsearch point-in-time snapshot, renewed with each page. Accepts a positive whole number followed by `ms`, `s`, `m`, `h`, or `d`, such as `120s` or `5m`. |

Index settings cannot contain whitespace. Point each setting at documents with its
matching schema; changing a name does not convert documents between schemas.

## What changed

- The reader previously always queried `appchi-v1` and `appchi-v2`. It now gets the
  source names from the loaded Elasticsearch settings for every scan.
- `/healthz` previously checked only the hardcoded v1 index. It now checks both
  configured sources and closes its Elasticsearch client after checking. Missing
  either source makes readiness fail, including when a particular team has no
  operators for that schema.
- Opening and renewing a point-in-time snapshot previously used a fixed `2m`
  lifetime. Both operations now use `ES_PIT_KEEP_ALIVE`.
- The mock generator, scale probe, and Kibana data-view lookup now use the same
  index settings. These scripts also load `.env`. Kibana looks up data views
  whose titles exactly match the configured source expressions.
- CLI commands now honor the existing registry and report-directory variables
  described below, alongside the web listener.

## Existing variables with broader support

These variables already existed; add them only if you need custom paths:

```dotenv
ADMIN_REGISTRY_PATH=config/teams.json
ADMIN_OUT_DIR=out
```

| Variable | Behavior after this change |
| --- | --- |
| `ADMIN_REGISTRY_PATH` | Used by the listener, `registry check`, `run`, `weekly`, `weekly-status`, and the scale probe. When unset or empty, service commands retain their packaged default registry; the scale probe retains its repository `config/teams.json`. |
| `ADMIN_OUT_DIR` | Sets the listener's output root and the CLI report root. `run` and `report` write `<root>/<first 16 characters of run_id>`; `weekly` writes `<root>/weekly`. Default root: `out`. |

Explicit `--registry` and `--out` flags override the corresponding environment
settings where those flags exist. `verify-acceptance` retains its own `--out`
option and `out/acceptance` default. Paths are resolved relative to the process's
working directory unless you supply absolute paths. Ensure the configured report
directory is writable by the service account.

## Applying the configuration

1. Keep the existing `ES_URL`, Elasticsearch credentials/CA, `SQL_*`, `LLM_*`, and
   `ADMIN_SECRET` configuration. This change introduces no replacement for them.
2. Add the three `ES_*` variables above to the deployment environment or local
   uncommitted `.env`, using the correct source names. Supply the same values to
   separate CLI and scheduler processes that should read those sources.
3. Set the two `ADMIN_*` path variables if custom registry or output locations
   are needed. Check existing values: CLI jobs now honor them too.
4. After deploying the changed application and configuration, restart the admin
   process so it loads the settings. Check authenticated `/healthz`; healthy
   responses have `ok: true` and `checks.elasticsearch.ok: true`.
5. Run a selected team's review and confirm that the v1 and v2 totals and findings
   appear as expected. For local mock verification, use the documented disposable
   `alerts_bi_test` database and deterministic fake model.

The process environment takes precedence over `.env`. No SQL migration is required.
The change has been implemented and tested; this guide does not indicate that it
has been merged or deployed.

## Local mock and verification notes

The mock generator requires two distinct concrete index names. Do not use aliases
or wildcard/read expressions as generator targets. Its `RESET=1` operation still
refuses non-loopback endpoints; use an isolated local mock when generating data.

Verification of the implementation passed 893 unit tests, 114 integration tests,
4 acceptance tests, and 843 acceptance-oracle checks, plus lint, formatting,
strict types, lockfile and packaging checks. A real CLI run and browser
Overview/Findings review were checked using custom source aliases and a `5m`
snapshot lifetime. Production behavior has not been verified.
