# Packaged execution runtime

`alerts-bi-admin` owns the unified UI source. It uses `alerts-bi-runs-runtime` in the same
process for analysis, API payloads, the run gate and generated reports. No second listener
or sibling checkout is required. Authentication and CSRF surround every mounted API route.

`runtime-source.json` records the upstream Git revision, immutable wheel hash and registry
artifact hash. The runtime exports tracked source from `venaTeam/alerts-bi-runs`; its
packaging changes the distribution name/version, pins the shared/operations versions and
omits console entry points. Engine code, prompts, rule versions and migration bytes are
unchanged. `alerts_bi_runs` remains its Python namespace.

To produce a new runtime artifact, update the explicit revision and version in
`scripts/vendor_runtime.py`, then run:

```powershell
python scripts/vendor_runtime.py C:/path/to/alerts-bi-runs
uv lock
uv sync --frozen
uv run pytest -q
```

The script refuses to overwrite a released wheel or a changed registry. Commit the new
wheel, manifest, matching registry, dependency pins and lockfile together. The shared and
operations wheels are separately versioned immutable artifacts; obsolete unreferenced
wheels are not needed to build this application.

The runtime distribution and the full `alerts-bi-runs` distribution share a Python
namespace. Install the admin app in its own environment; do not install both distributions
into one environment. SQL schema changes and weekly jobs remain owned by the runs repo.
