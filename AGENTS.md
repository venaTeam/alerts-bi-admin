<!-- eng-project-setup -->
## alerts-bi-admin: agent navigation

Unified operator UI, execution, migrations, weekly jobs, registry and shared/operations library production.

### Sources of authority

- design: `docs/upstream/alerts_bi_design.md` — Authoritative behavior; consult affected sections before behavior/boundary changes.
- design: `docs/upstream/alerts_bi_flow.md` — Approved runtime flow.
- design: `docs/upstream/alerts_bi_implementation_plan.md` — Implementation blueprint.
- design: `docs/upstream/outputs.md` — Output and API meanings.
- design: `docs/upstream/async-runs.md` — Durable manual submissions, worker/recovery, HTTP compatibility and output storage.
- design: `docs/upstream/backfill.md` — Operator multi-week backfill: selection, queued job states, worker claim order and weekly priority.
- deployment: `deploy/openshift/README.md` — The OpenShift procedure, Templates and value files; implements `docs/upstream/openshift-deployment.md`. Keep the Templates, examples and runbook in step (tests/unit/test_openshift_deploy.py).

### Verification

- unit: `["poetry", "run", "pytest", "tests/unit", "tests/engine/unit", "-q"]` in `.` (observed; pyproject.toml; executed results recorded in design/releases/).
- lint: `["poetry", "run", "ruff", "check", "."]` in `.` (observed; pyproject.toml; executed results recorded in design/releases/).
- format: `["poetry", "run", "ruff", "format", "--check", "."]` in `.` (observed; pyproject.toml; executed results recorded in design/releases/).
- types: `["poetry", "run", "mypy"]` in `.` (observed; pyproject.toml; executed results recorded in design/releases/).

### Project invariants

- Application code lives directly in src. Use relative imports within the app and src imports for local tests/scripts. Setuptools maps src to the service namespace in installed packages; preserve that namespace and independent builds.

- Read scoped instructions and source indexes first; consult the relevant canonical design, flow and blueprint for affected behavior or boundaries. Expand when dependencies or contradictions emerge. If required authority is unavailable, report the blocker.
- Design > approved flow > implementation blueprint > code. Read outputs.md before changing exports, report rendering or API contracts. Read both alerting guides completely for rule, scoring, prompt or alert-quality work.
- Use the pinned docs/upstream snapshot when this repository is cloned alone. It is an immutable input: propose product changes in alerts-bi-design and refresh the recorded revision/hashes. Do not silently edit snapshots or reinterpret approved behavior.
- If a parent AGENTS.md exists in a multi-repository workspace, read it explicitly for routing. The repository remains buildable and understandable without it. Parent instructions never become a runtime dependency.
- Inspect local changes before edits, preserve others work, and keep KeepHQ excluded. Do not install GitHub or Figma plugins. Use the selected main model.
- Use Python 3.12+, Poetry 2.3.1, committed poetry.lock, pytest, ruff and strict mypy. SQLAlchemy Core with pymssql targets real SQL Server; never SQLite or an in-memory integration substitute. No Node.js build/test dependency. Follow the README setup, including pip bootstrap for existing uv-created environments and the setuptools editable install.
- Keep one selected team per run, app+key identity, exact UTC windows, separate v1/v2 counts and SQL-only output rendering. Preserve R6 episode boundaries, suppression safety, LLM audit/retry behavior, prompt text, and deterministic identifiers.
- Do not edit applied SQL/revision files or regenerate the hand-authored expected-results oracle. Admin owns the sole migration chain. Changes to immutable prompt/catalog/instant formats require their approved version process.
- No secrets, alert documents or complete model payloads in normal logs or commits. Use environment settings and placeholders. Read deployment credentials only for the authorized task.
- Use eng-coordinate for cross-repository work and establish contracts before parallel implementation. At most three useful children, no recursive delegation, bounded ownership, one integrator. Subagents read the complete design and report files, commands, assumptions and actual results.
- Run the affected checks and coordinated contracts; report missing services and skipped checks honestly. Reset only the explicit local mock and disposable alerts_bi_test database. Never delete development or production data. Deployment, merges and external messaging need existing authorization.
- AGENTS.md is the only instruction body; CLAUDE.md stays a short @AGENTS.md pointer. Record accepted product decisions in canonical design in the same task, with a date, and reconcile affected flow/contracts.
- The console has no login and records no operator names (design "Admin console without login", 2026-10-19); preserve the ADMIN_SECRET-signed HMAC forms, cross-site rejection and append-only decisions, and do not reintroduce identities without a design decision. The UI calls local src/engine adapters; only engine modules use ES/model clients. No portal imports or sibling-checkout dependencies. Admin owns engine, migrations, registry and mock tooling. Preserve applied revision/SQL bytes and both local/installed src.db.ledger bridges. Shared stays free of app/operations imports; operations depends only on shared. Publish new immutable library versions for changes and update portal explicitly.

Use this project guidance with more specific area instructions. Requirements and decisions remain in their linked sources.
Preserve local changes; use the narrowest meaningful checks and report what actually ran. Setup does not grant deployment authority.
<!-- /eng-project-setup -->
