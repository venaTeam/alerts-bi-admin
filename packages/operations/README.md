# Alerts BI operations library

Registry validation, pure panel parsing, publication and decision commands, and the SQL-only
scorecard/export renderer used by admin execution and operator workflows. The registry JSON Schema is packaged as
a resource so validation works from an installed wheel without a source checkout.

This library has no run pipeline, Elasticsearch, model client, HTTP application or migrations.
The read-only portal must not install it. Admin owns the single executable migration chain.

Build from the admin repository with `uv build --package alerts-bi-operations`. Consumers pin
both this wheel and the matching `alerts-bi-shared` release.
