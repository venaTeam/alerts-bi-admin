# Alerts BI: application and design repository separation plan

**Date:** 2026-10-06  
**Status:** approved for implementation on 2026-10-06; see `../releases/` for delivered revisions and evidence.  
**Authority:** [canonical design](alerts_bi_design.md), especially sections 7.9–7.15.  
**Inspected source:** `c518eeaeb5a3ecb35b5348828300454379e7d535` on `codex/stream-weekly-events`, with an existing uncommitted edit to `AGENTS.md`. That edit is user-owned and preserved in the original checkout. Implementation uses this inspected source revision and the approved documentation changes.

## 1. Requested result and recommendation

Keep three application repositories and add a dedicated design/contracts repository in one local parent directory. This revised recommendation incorporates the user's 2026-10-06 suggestion of a separate design repository; it supersedes the initial proposal to keep system design in runs.

- `alerts-bi-design`: canonical product design, architecture decisions, versioned contracts and cross-repository compatibility records.
- `alerts-bi-runs`: the complete analysis service, including manual/HTTP triggers, weekly scheduling, persistence, migrations and scorecard generation.
- `alerts-bi-portal`: the reader application for published weekly reviews.
- `alerts-bi-admin`: the operator application and manual publication/decision commands.

The parent has a local `AGENTS.md` outside Git. All four repositories have their own tracked `AGENTS.md`. Each application has its own dependencies, tests, image and release; design has documentation/contract checks and versioned releases, with no application image. The existing product behavior remains binding.

**Recommended boundary:** design owns the agreed behavior and interface definitions; application repositories own their implementation. Retain the shared SQL Server database and extract narrowly scoped, versioned Python libraries for code used by multiple applications. Host those libraries in `alerts-bi-runs`; no additional shared-code repository is proposed. The applications communicate through the existing database contracts. No new internal HTTP service or queue is required for this separation.

This gives independent application builds and deployments, with explicit compatibility requirements for the shared database and libraries. It does not make database evolution independent across the three applications.

### Alternatives considered

| Approach | Consequence | Recommendation |
|---|---|---|
| Three application repositories plus design/contracts, with pinned shared libraries and the existing SQL database | Gives system requirements a neutral home while preserving one implementation of common behavior | Use this approach |
| Three repositories with system design retained in runs | Fewer repositories, but system authority is coupled to one implementation | Superseded recommendation |
| Copy shared modules into all three application repositories | Starts quickly, but publication, summary and security fixes can diverge | Avoid for business logic |
| An additional shared-code repository | Gives shared code a separate lifecycle | Defer; the design repository has a different responsibility |
| Separate databases with service APIs/events | Changes persistence, transactions, publication and failure behavior substantially | Separate architecture project |
| Install the entire runs application into portal/admin | Keeps almost all current dependency coupling | Does not meet the intended separation |

## 2. Local directory and Git layout

Suggested parent: a new `alerts-bi-workspace` directory alongside the current checkout. The folder name is a proposed default, not an established path.

```text
alerts-bi-workspace/                   no .git here
  AGENTS.md                            local workspace routing; never committed
  CLAUDE.md                            optional local pointer to AGENTS.md
  WORKSPACE.json                       local paths/revisions/coordination index
  alerts-bi-design/
    .git/
    AGENTS.md                          tracked, design/contracts-specific
    CLAUDE.md                          tracked pointer to AGENTS.md
    README.md                          system map and reading order
    docs/                              canonical design, flow, output meanings, guides
    decisions/                         accepted decisions and clearly marked proposals
    contracts/                         SQL/API/registry/output interface definitions
    releases/                          supported contracts and tested app revisions
    scripts/                           contract validation and workspace templates
  alerts-bi-runs/
    .git/
    AGENTS.md                          tracked, runs-specific
    CLAUDE.md                          tracked pointer to AGENTS.md
    README.md
    pyproject.toml
    uv.lock
    Dockerfile
    src/alerts_bi_runs/
    packages/
      shared/                          separately built alerts-bi-shared wheel
      operations/                      separately built alerts-bi-operations wheel
    config/
    scripts/
    tests/
    test/fixtures/
    docs/
  alerts-bi-portal/
    .git/
    AGENTS.md
    CLAUDE.md
    README.md
    pyproject.toml
    uv.lock
    Dockerfile
    src/alerts_bi_portal/
    tests/
    docs/
  alerts-bi-admin/
    .git/
    AGENTS.md
    CLAUDE.md
    README.md
    pyproject.toml
    uv.lock
    Dockerfile
    src/alerts_bi_admin/
    tests/
    docs/
```

The four `.git` directories are siblings. Do not initialize Git in the parent, use submodules, or put the new repositories inside the current Git checkout. A child cannot track its parent's `AGENTS.md`; there is no need to ignore every file named `AGENTS.md`, which would also hide the required repository instructions. Verify the parent is not itself inside another Git worktree before creating the layout.

Keep the existing `alerts BI` checkout available through validation and cutover. The subsequent implementation request authorizes creating and publishing the four repositories in the same organization. The original checkout remains intact; deployment and deletion are outside this extraction. KeepHQ is outside the workspace and all migration work.

Each application must install and run from a standalone clone. Production builds must not depend on sibling paths, a parent virtual environment, `PYTHONPATH` modifications, or the local parent instruction file. Applications have separate virtual environments and ignored secrets; design uses its own validation environment only when needed. The local workspace index is a convenience; durable contracts and release evidence remain in the design repository.

## 3. What the current code requires us to separate

The application surfaces exist, but their packaging is shared. [The current package](https://github.com/venaTeam/alerts-bi-runs/blob/c518eeaeb5a3ecb35b5348828300454379e7d535/pyproject.toml) installs one distribution whose import name is `src`; [the Dockerfile](https://github.com/venaTeam/alerts-bi-runs/blob/c518eeaeb5a3ecb35b5348828300454379e7d535/Dockerfile) builds one image for all commands.

| Current area | Destination / action |
|---|---|
| `src/api`, `src/run`, `src/es`, `src/llm`, `src/weekly` | Runs application |
| Deterministic evaluation in `src/domain`, `src/rules`, suppression evaluation/streaming | Runs application; extract only constants/types/parsing genuinely needed elsewhere |
| `src/db/migrations`, `migrate.py`, `ledger.py`, `llm_audit.py`, `reader.py` | Runs owns the only migration chain, audit persistence and legacy reader-grant utility |
| `src/db/repositories.py` | Split pipeline writes/cache access from committed report-read functions; do not move the entire module into a shared dependency |
| `src/portal` | Portal routes, view queries and pages; first extract presentation functions that admin currently imports |
| `src/admin` | Admin routes, authentication, queries, internal summary adapters and pages |
| `src/review` | Operations library, used by runs scheduling and admin actions/CLI |
| `src/report` and its SQL loaders | Operations library, preserving one renderer for runs and admin scorecards |
| `src/insights`, shared chart/summary presentation, catalogue labels | Shared library, with no application-route imports |
| `src/config`, `src/cli.py` | Split by application; share only generic environment/SQL primitives |
| `src/registry.py`, registry schema, panel parser needed by admin explanations | Operations library; package the pinned schema from design; authoritative registry data remains in runs |
| `scripts`, mock Compose stack, hand-authored acceptance oracle | Runs; reuse the current fixture system |
| Canonical design, flow, blueprint, output meanings, alerting guides and binding specifications | Design repository; preserve their authority and historical decisions |
| Tests and application operating documentation | Move with their application/library owner; design coordinates cross-application contract checks |

Specific seams discovered in the source:

- [Admin pages](https://github.com/venaTeam/alerts-bi-runs/blob/c518eeaeb5a3ecb35b5348828300454379e7d535/src/admin/pages.py) and [admin summary](https://github.com/venaTeam/alerts-bi-runs/blob/c518eeaeb5a3ecb35b5348828300454379e7d535/src/admin/summary.py) import portal styles, explanation functions, charts, HTML helpers, summary rendering and `daily_points`. These must become pure shared presentation functions. Admin must not install the portal app just to render a page.
- [Weekly scheduling](https://github.com/venaTeam/alerts-bi-runs/blob/c518eeaeb5a3ecb35b5348828300454379e7d535/src/weekly/runner.py) calls the same publication function as [admin](https://github.com/venaTeam/alerts-bi-runs/blob/c518eeaeb5a3ecb35b5348828300454379e7d535/src/admin/app.py). Keep that one transactional implementation in a shared operations package.
- [Portal settings](https://github.com/venaTeam/alerts-bi-runs/blob/c518eeaeb5a3ecb35b5348828300454379e7d535/src/config/portal.py) load the full pipeline `AppConfig`; [admin settings](https://github.com/venaTeam/alerts-bi-runs/blob/c518eeaeb5a3ecb35b5348828300454379e7d535/src/config/admin.py) also contain it. Give each application its own settings so portal/admin do not validate or require ES/LLM configuration.
- [The common CLI](https://github.com/venaTeam/alerts-bi-runs/blob/c518eeaeb5a3ecb35b5348828300454379e7d535/src/cli.py) imports the pipeline and model modules before selecting a command. Separate entry points are necessary; moving only the web folders would leave this coupling.
- [Registry loading](https://github.com/venaTeam/alerts-bi-runs/blob/c518eeaeb5a3ecb35b5348828300454379e7d535/src/registry.py) and [prompt construction](https://github.com/venaTeam/alerts-bi-runs/blob/c518eeaeb5a3ecb35b5348828300454379e7d535/src/llm/prompt.py) depend on working-directory-relative resources. Install schemas/guides as package resources without changing their content, while keeping the registry data path explicitly configurable.
- [The portal boundary test](https://github.com/venaTeam/alerts-bi-runs/blob/c518eeaeb5a3ecb35b5348828300454379e7d535/tests/unit/test_portal_surface.py) checks direct imports in portal files. Add a transitive installed-package check after extraction, so shared libraries cannot indirectly reconnect the portal to operator writes or the pipeline.

## 4. Shared libraries and dependency direction

Build two internal libraries from `alerts-bi-runs`:

| Library | Contents | Consumers | Excluded |
|---|---|---|---|
| `alerts-bi-shared` / `alerts_bi_shared` | Generic SQL connection and environment primitives, safe logging/time formatting, stable catalogue/phase labels, pure insights, shared HTML/chart/summary helpers | All three apps and the operations library | Application routes, migrations, base-table report queries, publication/decision operations, ES and LLM clients, orchestration |
| `alerts-bi-operations` / `alerts_bi_operations` | Publication/decision transactions, registry validation/schema, reused panel parsing, committed report queries and renderer | Runs and admin | Run execution, weekly orchestration, ES/model clients and migrations |

Dependency direction is acyclic: `shared` has no application dependency; `operations` depends on `shared`; runs and admin depend on both; portal depends only on `shared`. Neither consumer imports another application's package. Keeping the libraries in runs makes that repository the upstream producer without making its running service a dependency of the other applications.

The SQL connector is a general database primitive, not a security boundary. Portal isolation still comes from approved queries, routes, import boundaries and network controls; the existing shared SQL login may have broader permissions.

Extract neutral presentation types/constants from `portal/pages.py` before moving charts, slides and summary rendering. Keep portal-specific links/routes and SQL queries in portal. Move the pure `daily_points` transformation out of `summary_queries.py`, leaving its database access in portal. Admin's reconstruction of supplied panel filters must use the same parser version as runs. Shared presentation must preserve distinct portal/admin disclosure rules.

Keep dependencies minimal: portal needs its web and SQL dependencies plus the shared library, with no Elasticsearch SDK, OpenAI SDK, Alembic, operator operations or registry payload. Admin adds operations/registry dependencies and its auth settings, but no analysis clients. The runs image owns those analysis and migration dependencies.

Use distinct import namespaces; three distributions all installing `src` would collide. Stage the rename separately from behavior changes. Preserve a minimal legacy import bridge only where immutable historical migration modules still reference `src.db.ledger`; do not edit already-applied revisions to accomplish a package rename. Test migration discovery and ledger verification from the installed runs wheel.

Pin library releases in each consumer's committed lockfile. Build immutable wheels with recorded hashes and make them available through the organization's existing artifact delivery mechanism. A local wheel directory can exercise the plan before a remote package host is selected. Do not assume a private package index already exists. Sibling editable installs may be an explicit developer convenience, never the committed production dependency source.

Changing a shared library requires its tests and affected consumer contract checks. A portal-only presentation change should require only a portal release. Do not ship the entire common package surface merely to avoid separating an import.

## 5. Database, registry and version contracts

**One database and one migration owner.** Runs owns all existing SQL revisions `001`–`008`, Alembic ordering and the checksum ledger, including portal views and operator tables. Moving code is not a reason to copy, reset or split the database. Preserve applied SQL and revision files, ordering, IDs and ledger values. Use additive future revisions when a real schema change is needed.

Portal continues querying only `portal_*` views against the pipeline's exact `SQL_*` database/login. Admin reads completed runs and operator records and calls operations-library writes. Preserve any documented admin database override during extraction, with the normal deployment targeting the same database. Do not silently restore separate portal credentials or invent a database permission guarantee the current deployment cannot provide.

Only the runs migration artifact is allowed to apply DDL. Deployment has one migration step, using the runs image/init container or the database-owning team's existing procedure. Portal/admin must not each introduce an independently maintained migration tree or race their own startup migrations. They check readiness against their required query contract; portal readiness must use its allowed views, not gain access to migration/audit tables.

Document a versioned SQL contract in design, with columns, types/nullability, publication filtering, ordering, identity semantics and write invariants. Runs implements it through the sole migration chain; contract checks compare the migrated database against the approved definition. Initial contract corresponds to the inspected schema head `008_measurement_basis`. Future evolution uses expand/migrate/contract: add compatible fields/views, update consumers, remove old surfaces only after the supported release combinations no longer use them. Missing/incompatible contracts refuse readiness with a redacted diagnostic; they never fall back to base tables or start auto-migration in consumers.

**One registry source.** Runs maintains `config/teams.json` and its versioning. Admin receives the same deployed registry artifact/ConfigMap to list every team, including teams with no runs. Portal uses published SQL data and needs no registry file. The admin repository must not carry an independently edited production registry. Local orchestration may explicitly mount the runs registry into admin; a standalone admin deployment receives that artifact through configuration. Preserve the complete-file hash and selected-entry snapshot in stored runs.

The registry JSON Schema belongs to the design contract release; registry values and deployment remain in runs. The operations package includes a verified copy of that released schema. Likewise, runs packages the approved alerting guides from a pinned design revision without changing their text. No runtime fetch or sibling checkout is required. Changing a guide still requires the existing prompt-version process; updating a design dependency must not silently rewrite an immutable prompt.

**Keep product versions separate from deployment versions.** Ruleset, prompt, parser and analysis application versions retain their existing meanings. In particular, `APP_VERSION` participates in deterministic run identity. A portal release or packaging rename must not alter it. Preserve the analysis version during parity verification; any later bump is deliberate and documented. Record application releases, shared-wheel hashes, source commits and schema head in a compatibility manifest, not by changing the run-ID formula.

The first release record, stored in design, must identify the exact runs/portal/admin commits, both shared-library versions/hashes, schema revision, registry revision/hash and design/contract source revision. A list of branch names is insufficient evidence that they work together. The three applications remain the deployable units; design is a versioned development/build input, not a running service.

### Design repository scope and contract changes

| Design owns | Implementation remains with |
|---|---|
| Product requirements, canonical architecture, runtime flow, cross-app roadmap and accepted decisions | Each application's source, local source map and operating instructions |
| SQL view columns/types/nullability and publication/decision semantics | Runs migrations; operations transaction implementation; consumer queries |
| Approved HTTP wire contracts and output filenames/columns/meanings | Runs Pydantic/OpenAPI generation and report rendering; each app's routes |
| Registry JSON Schema and alerting standard documents | Runs registry values; packaged, pinned copies in the consuming libraries/application |
| Contract versions, compatibility matrix and cross-repository release records | App/library lockfiles, images and repository-specific test results |
| Workspace instruction templates and routing specification | Generated parent AGENTS.md and WORKSPACE.json, which stay local outside Git |

Move existing authoritative documents first, preserving decisions and wording. Do not create a second acceptance oracle or a generic schema for every internal Python object. Define contracts only at actual boundaries: SQL views/operator transactions, trigger API, registry format, report exports, and supported shared-library interfaces.

For an HTTP contract, retain code-generated OpenAPI in runs and compare it with a reviewed, versioned contract snapshot in design. Generate candidate updates from code rather than editing two competing wire schemas. The same principle applies to checking SQL metadata and CSV headers against the approved contract. A mismatch is a compatibility decision to resolve, never permission to update expected contracts until tests pass.

Contract change sequence:

1. Propose the change in design with affected producers/consumers, compatibility expectations and migration/rollback notes. Clearly mark draft versus accepted behavior.
2. Review and approve the contract change before treating it as authoritative. Tag or otherwise freeze its immutable source revision; publishing that release still needs authorization.
3. Implement it in the owning application/library and update affected consumers' pinned contract references. Compatible consumers may stay on their previous supported version.
4. Run the coordinated checks against explicit app commits, schema and contract versions, using the existing runs fixture system. Record successful combinations separately from proposed combinations.
5. Follow the dependency-aware deployment order. A design commit alone neither ships code nor changes a deployed contract.

Each application records its design commit, contract version and artifact hashes in a small tracked lock/manifest. Never follow a mutable `main` branch at build/runtime. Design changes with no runtime or interface effect do not force three application releases. If the local design checkout is newer than an application's pin, identify the difference before work; use the pin for release verification and the approved target revision for a coordinated upgrade.

## 6. Agent instructions and documentation ownership

### Parent `AGENTS.md`: local routing and coordination

Keep this short. Its proposed content is:

- Workspace purpose and the four relative repository paths.
- Design owns system requirements, decisions and contract definitions; runs owns analysis, database migrations, shared libraries and registry data; portal owns reader behavior; admin owns operator behavior. Named human maintainers remain unknown until assigned.
- Read the target repository's `AGENTS.md` and required design before repository work. For a cross-repository task, inspect each affected revision and instruction file.
- Run Git, dependency and test commands in the named child directory. There is no parent Git repository or shared application environment.
- Identify the contract producer and consumers before cross-repository edits; keep one integrator and bounded file ownership if delegation is used.
- Record compatible revisions and run the relevant integration checks before declaring a coordinated change complete.
- Preserve local changes; leave KeepHQ outside scope; keep secrets local; publishing and deployment need task authorization.

The parent file is navigation and coordination guidance. It must not become a second copy of product rules, the full design, or all repository instruction files combined. Do not install it as the user's machine-wide `~/.codex/AGENTS.md`.

### Tracked repository `AGENTS.md` files

Each repository file contains its own purpose, source map, design authority, dependency boundaries, commands with working directory, test prerequisites, and completion requirements:

| Repository | Key repository-specific instructions |
|---|---|
| Design | Full canonical design first; distinguish draft, accepted, implemented and verified states; name contract producers/consumers and versions; preserve decision history; validate contracts against implementation; no application migrations or production registry values |
| Runs | Full pinned design first; flow/blueprint and both guides for the relevant work; one-team/time/identity/LLM/output invariants; sole migration and registry-data authority; unchanged acceptance oracle; SQL Server integration and clean mock acceptance |
| Portal | Full pinned design first; published views only; read-only methods; weekly totals; no internals, pipeline, model, operator writes or registry dependence; network allowlist and script-free rendering; contract and browser checks |
| Admin | Full pinned design first; loopback/proxy identity and HMAC/same-site protections; append-only audited decisions; publication rules through the common operations package; completed-run disclosure is operator-only; auth, transaction, SQL and browser checks |

Each tracked file explicitly says to read `../AGENTS.md` when that local workspace file exists, as additional workspace context. Do not rely on automatic discovery above a child Git root. A standalone clone remains usable when it is absent; the repository file contains every mandatory project requirement. Conflicting workspace/project guidance must be resolved against the canonical design and current user request.

Keep `CLAUDE.md` as a short `@AGENTS.md` pointer in each repository. Adapt `test_instruction_files.py` to check each repository's real responsibilities and design pointer; remove its current assumption that every instruction file must exceed 100 lines. Do not replace it with four copies of the current large file.

### Where the authoritative design lives

Move the full canonical design and cross-application contracts to `alerts-bi-design`. All three application repositories carry an immutable, hash-checked snapshot at `docs/upstream/alerts_bi_design.md` with the source commit and hash, so agents can read the complete approved design even from a standalone clone. These are mechanically synchronized reference snapshots, never separately edited design authorities. Their own docs explain application-specific structure and operating commands. When the design checkout is available locally, read the matching pinned revision there; do not silently substitute its current branch tip.

During snapshot generation, preserve all design wording and rebase relative links to the immutable canonical source revision. Record both the original source hash and the generated snapshot hash; otherwise links to scripts and companion documents would break in a consumer checkout. Pin referenced binding specifications with the same source revision.

Product changes are proposed in the canonical source, approved there, then intentionally propagated to consumer snapshots and contracts. During extraction update obsolete module names, command locations and deployment examples in the flow, blueprint, output documentation and handoffs without changing product meanings. Read `docs/outputs.md` in full before moving/editing report or API contract code, and both guides in full before any rule/prompt/scoring change.

`WORKSPACE.json` records local relative paths, observed revisions, contract edges, integration owner and commands. It is local like the parent instructions. Keep the portable template/bootstrap procedure in design; the generated active parent file stays outside Git. Local paths, private overrides and secrets must not be copied into the tracked template. Test instruction navigation both from the workspace root and from each of the four child checkouts before declaring setup complete.

## 7. Implementation sequence and checkpoints

| Phase | Work and ownership | Exit criterion |
|---|---|---|
| 1. Freeze the source | Integrator reconciles the inspected branch with the intended release baseline, preserves the local AGENTS edit, records source revision and existing behavior | A reviewed source checkpoint, module/test ownership map and baseline verification results |
| 2. Establish design authority | Create the non-Git workspace and design repository from the frozen design/contract sources; preserve provenance, fix moved links and document producer/consumer ownership | A reviewed, immutable baseline contract revision and a local workspace routing file |
| 3. Establish package boundaries | In an isolated application checkout, extract shared/operations libraries, separate settings and CLIs, split read/write repositories and remove admin-to-portal imports | Original behavior works with the new package boundaries; no consumer imports the pipeline indirectly |
| 4. Extract the applications | Create the three application checkouts beside design; migrate owned paths/histories and add each repository's instructions and contract pin | Four distinct Git roots; local parent instructions and tracked child instructions; no secrets or unrelated edits lost |
| 5. Prove standalone builds | Give each app its own lockfile, README, entry point and Dockerfile; package runtime resources; install pinned wheels | Each app builds/runs without the other source checkouts or a repository-root working directory |
| 6. Establish contract verification | Design owns contract definitions and the coordinated harness; runs owns shared-library tests/fixtures; each app owns its unit/integration tests and browser acceptance | Verified release manifest for the exact design and three app revisions, artifacts, schema and registry |
| 7. Rehearse locally | Use the existing ES/SQL mock, generate/persist a run, publish/decide via admin, read it in portal, then withdraw | End-to-end results and access boundaries match the current application |
| 8. Prepare cutover | Update deployment documents/configuration to three images; check migration readiness, scheduler handover and rollback | Reviewable deployment changes and evidence; deploy only under separate authorization |

Do not combine this extraction with new rules, prompt changes, queueing, a frontend rewrite, additional replicas or historical backfill. If a necessary refactor exposes a correctness/security conflict, stop the affected change and resolve it against the design while continuing unrelated extraction work.

### Git history and source handling

Recommended default: preserve the existing repository history in runs; create design/portal/admin with an initial import recording the exact original commit and source-path mapping. Preserve the design documents' existing decision history and links to original revisions. Retain the original Git history for provenance. If full per-file history in each new repository is required, choose history-preserving extraction before creating their first commits. Never rewrite the active source checkout's history. Remote hosting, repository owners/access and artifact hosting are infrastructure choices to settle before publishing; no host is assumed by this plan.

### Commands after separation

Proposed non-colliding console scripts are `alerts-bi-runs`, `alerts-bi-portal` and `alerts-bi-admin`. Runs retains `run`, `serve`, `weekly`, `weekly-status`, `report`, `registry`, `db` and acceptance verification. Admin owns `serve`, `publish`, `unpublish`, `publications`, `decide` and `decisions`. Portal only starts its reader service.

Retain `alerts-bi` as a runs-only compatibility alias for existing run/schedule/db automation during transition. Do not install three different executables with that same name into one environment. Move operator CLI invocations and portal/admin startup commands explicitly in documentation and deployment configuration. A removed command must report its new location, not silently perform a different operation. Preserve existing HTTP paths, payloads, status codes, output filenames and default ports (8000/8100/8200).

## 8. Verification and acceptance criteria

The following are **planned checks**, not results of this planning session.

1. **Build isolation:** install each built wheel and container from a clean environment, outside its source directory. Portal/admin have no ES/LLM dependency or required settings. Portal has no operations library. Verify schema, guide and SQL resource inclusion, unique import namespaces, and the migration compatibility bridge.
2. **Repository checks:** use each repository's declared commands and all affected tests. Current baseline commands are `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, `uv run pytest tests/unit`, `uv run pytest tests/integration`, and `uv run pytest tests/acceptance`. Consumer paths/suites will change during extraction; document actual replacements. Run checks for both shared distributions as well as application code.
3. **Real persistence:** test fresh migrations and an existing database at revision 008; compare schema/ledger and stored reads after extraction. No SQLite substitute. Confirm only runs owns DDL and applied migrations remain unchanged.
4. **Analysis parity:** use the existing seeded generator and deterministic model against the hand-authored oracle. Compare identities, rules, metrics, representatives, prompt/request hashes, retries, audit replay, run IDs and four output artifacts. Packaging changes must not rewrite catalogues, prompt text, instant formatting or the oracle.
5. **Cross-app flow:** one team run remains invisible before publication; admin publishes it; portal shows the expected published week/totals/evidence; a human decision remains separate from machine quality; withdrawal hides the week; published runs refuse replacement. Include overlap/gap/replace refusal and a concurrent schedule/admin publication attempt to exercise the shared SQL locking behavior.
6. **Weekly behavior:** preserve Monday windows, onboarding, ordered catch-up, health holds, storing later weeks, forced publication after three days with a reader note, expiry gaps, outcomes and the existing schedule lock. Only one scheduler is active during cutover.
7. **Reader/operator separation:** portal remains GET/HEAD-only with the existing allowlist, CSP, escaping and safe links; unpublished data and service internals never appear. Admin rejects missing identity, forged/cross-site requests and another user's token; actions record the authenticated operator. Trigger still refuses a second concurrent HTTP run with 409.
8. **Rendered UI:** open the trigger, portal and admin against the same mock state; inspect the run flow, summary/slides, findings, pagination, forms and failures at relevant viewport sizes. Preserve styles and navigation while removing cross-application imports.
9. **Compatibility:** validate design's schema/contract files, document links and source provenance; run consumer contract tests against the selected schema and supported old/new shared releases. Compare generated API contracts, SQL view metadata, registry schema and export headers with the pinned definitions. For a future additive database change, test the prior supported consumers before migrating, then the new consumers. Record exact design/app/artifact revisions and test outcomes in design's release manifest.
10. **Agent navigation:** verify parent instructions are outside all four Git roots; each child tracks its AGENTS file and pointer, links resolve, pinned design hashes match, commands use the correct directory, and a standalone clone needs no private local instructions. A newer sibling design checkout must not silently override a pinned contract.

Split the existing tests by ownership rather than copying the whole suite into each repository. Publication/decision and report tests follow operations; pure insights/catalogue/presentation tests follow shared; portal/admin suites follow their applications. Extract the cross-surface assertions currently embedded in integration suites into the coordinated harness owned by design. That harness selects exact revisions/artifacts and invokes each app's checks in its own environment; it reuses fixtures supplied by runs. Shared test helpers can be packaged as test-only fixtures; do not add runtime dependence on another repository's `tests` tree or invent another mock dataset. Design's validation tooling does not import application implementations into a production package.

The coordinator provisions and resets the disposable fixture database once per serialized test lane. Existing suites reset `alerts_bi_test`; running three such suites concurrently against that same database would invalidate results. Validate the explicit local mock endpoints and disposable target before any clean reload/reset, preserve persistent Docker volumes and `alerts_bi_dev`, and use a stable Compose project/volume identity when the files change location. Missing services and skipped acceptance tests are unmet release gates, not passes.

## 9. Deployment order and rollback

1. Freeze the approved design/contract revision, then build and verify the shared artifacts and three application images against it; record immutable versions/digests and the compatible database/registry contract. Design has no application deployment.
2. Back up/check the database using the existing operating procedure. For a pure extraction, the target is no DDL or data change. If an approved additive migration is necessary, apply it once from the runs migration artifact before consumer rollout.
3. Deploy compatible portal/admin images and perform read/auth smoke checks. Preserve the admin proxy sidecar and loopback-only listener. Provide SQL settings to both, registry and admin secrets to admin, and ES/LLM credentials only to runs.
4. Drain in-flight analysis and stop the old schedule before switching the runs trigger and weekly job to the new image. Preserve the existing single trigger replica/worker constraint: its HTTP gate is process-local, and the weekly SQL lock is not a universal manual-run lock.
5. Verify the cross-app flow and resume exactly one weekly scheduler. Record results before retiring any original deployment.

Rollback restores the prior compatible image set and scheduler configuration; stop the new scheduler before re-enabling the old one. Preserve database records, completed runs, audit journals, publications and decisions. Do not downgrade applied SQL migrations or reset the database to roll back a code extraction. Retain the original checkout/artifacts until the agreed validation period is complete.

OpenShift examples are currently proposals, not cluster-tested deployment evidence. This split does not establish that they work on a cluster or resolve unrelated existing infrastructure gaps.

## 10. Decisions to settle before implementation/publishing

The requested three application boundaries, common local parent, local parent `AGENTS.md` outside Git, and tracked instructions in each repository remain. The revised recommendation adds the user's suggested fourth repository for design/contracts and moves system authority out of runs. This is still a plan; creating the repositories and activating their instructions is implementation work.

- Confirm the source release checkpoint and treatment of in-progress work before extraction.
- Accept or revise the recommended design/contracts ownership, shared-library approach and suggested directory/repository names.
- Choose design/portal/admin history preservation if the provenance-recorded initial import is insufficient.
- Before publishing, supply the Git destinations/access owners and shared artifact delivery location.

No repository split, instruction activation, remote creation, dependency installation, database operation or deployment was performed while preparing this plan. Source/layout/imports, documentation and migration ownership were inspected; application and cluster verification remain work for the implementation phases.
