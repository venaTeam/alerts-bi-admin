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
that shared library independently and has no operations dependency. Version 0.2.7 records no
operator names: publications, withdrawals and decisions no longer take or store a person, and
`publish_run` refuses a `published_by` other than "" or the scheduler's `weekly-schedule`.
Version 0.2.8 pins alerts-bi-shared 0.1.9, whose console log format it uses for its own records.
Version 0.2.9 pins alerts-bi-shared 0.1.10, which can also forward those records to Elasticsearch.
