# 003 — Consolidate execution and operations in admin

Accepted by the product owner, 2026-10-12. Supersedes the runs ownership in decisions 001/002
and the runtime-wheel boundary accepted on 2026-10-08. Admin owns maintained engine source,
migrations, scheduling, registry, fixtures and shared library production. Portal stays a
separate read-only app; design stays authoritative. No schema, scoring or prompt change.

See [the retirement contract and cutover](../docs/runs-retirement.md) and
[the source map](../docs/source-map.md). Deployment and repository archival need separate
authorization and operational evidence.
