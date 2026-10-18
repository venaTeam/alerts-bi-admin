# Alerts BI operations library

Registry validation, pure panel parsing, publication and decision commands, and the SQL-only
CSV export renderer used by admin execution and operator workflows. The registry JSON Schema is packaged as
a resource so validation works from an installed wheel without a source checkout.

This library has no run pipeline, Elasticsearch, model client, HTTP application or migrations.
The read-only portal must not install it. Admin owns the single executable migration chain.

Build from the admin repository with `poetry --directory packages/operations build --output ../../dist`. Consumers pin
both this wheel and the matching `alerts-bi-shared` release.

Version 0.2.0 removes the standalone HTML scorecard renderer and export. The three CSV
formats and columns remain unchanged. Version 0.2.6 uses alerts-bi-shared 0.1.8; the portal consumes
that shared library independently and has no operations dependency.
