# Repository source map

Current ownership after the approved 2026-10-12 retirement. Historical `src.*` engine paths
now live under admin's `src/engine`; the installed namespace is `alerts_bi_admin.engine`.

| Owner | Source | Responsibility |
|---|---|---|
| admin | src/app.py, pages.py, assets.py, auth.py, queries.py, summary.py | Unified authenticated operator UI |
| admin | src/cli.py | UI, operator, engine, migration and weekly commands |
| admin | src/engine/api, run, es, llm, weekly | Execution adapters, analysis and scheduling |
| admin | src/engine/domain, rules, suppression | Deterministic analysis |
| admin | src/engine/db | Persistence and the sole migration chain |
| admin | src/db/ledger.py and compat/src/db/ledger.py | Applied migration imports in local and installed environments |
| admin | packages/shared | Separately versioned SQL primitives and pure insights/presentation |
| admin | packages/operations | Registry, operator transactions, reports and parser |
| admin | config, scripts, test/fixtures, tests/engine | Registry, original mock tooling and acceptance coverage |
| portal | src | GET-only published views and reader pages |
| design | docs, decisions, contracts, releases | Requirements and compatibility evidence |

Supported executables are `alerts-bi-admin` and `alerts-bi-portal`. Admin is independently
buildable using its committed Poetry lock; portal pins an immutable shared wheel produced by
admin. Shared 0.1.5 adds pooled web SQL and bounded summary readers; operations 0.2.2 pins it. The earlier operations 0.2.0 removes `report.html` and restricts
the public report renderer to the three CSV exports. No supported
consumer imports or installs a runs package. The old repository remains historical.

Performance work: [bounded read contract](read-path-performance.md). Admin owns migrations 012–013, src/engine/db/read_models.py, src/engine/run/checkpoint.py, and scripts/benchmark_reads.py. Shared owns read/paging contracts; portal consumes only published views.
