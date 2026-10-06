# Repository source map

The runtime flow and implementation blueprint retain their approved behavior. Historical
`src.*` references describe the extraction source; current import ownership is below.
All repositories are under [venaTeam](https://github.com/venaTeam).

| Owner | Current source | Responsibility |
|---|---|---|
| alerts-bi-runs | src/alerts_bi_runs/api, run, es, llm, weekly | Triggering, analysis and scheduling |
| alerts-bi-runs | src/alerts_bi_runs/domain, rules, suppression | Deterministic analysis |
| alerts-bi-runs | src/alerts_bi_runs/db | Persistence and the sole migration chain |
| alerts-bi-runs | src/src/db/ledger.py | Historical migration import compatibility only |
| alerts-bi-runs | packages/shared/src/alerts_bi_shared | SQL primitives, pure UI/insights, stable labels |
| alerts-bi-runs | packages/operations/src/alerts_bi_operations | Registry, operator transactions, reports, parser |
| alerts-bi-runs | scripts, test/fixtures, tests | Existing mock generator and acceptance oracle |
| alerts-bi-portal | src/alerts_bi_portal | GET-only published views and reader pages |
| alerts-bi-admin | src/alerts_bi_admin | Proxy-authenticated operator pages and commands |
| alerts-bi-design | docs, decisions, contracts, releases | Canonical intent and tested compatibility |

The executable names are `alerts-bi-runs`, `alerts-bi-portal`, and `alerts-bi-admin`.
The legacy `alerts-bi` executable remains with runs and explains moved commands.
Each application's README and lockfile define its current operating/build procedure.

Shared-library changes require new distribution versions and explicit consumer wheel
updates. A design update requires a committed canonical revision and regenerated pinned
snapshots. Neither procedure changes rule/prompt/parser/analysis versions implicitly.
