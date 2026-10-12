# Alerts BI shared library

Versioned Python values, SQL connection configuration, summary calculations and pure HTML
presentation used by the admin and portal applications. It has no application imports, registry or
operator write operations. SQL consumers supply the existing `SQL_*` configuration.

Build from the admin repository with `uv build --package alerts-bi-shared`. The root lockfile
pins the build's dependency set; consuming applications pin the published wheel and its hash.
