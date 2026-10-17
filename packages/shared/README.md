# Alerts BI shared library

Versioned Python values, SQL connection configuration, summary calculations and pure HTML
presentation used by the admin and portal applications. It has no application imports, registry or
operator write operations. SQL consumers supply the existing `SQL_*` configuration.

`SQL_ENCRYPT=true` makes every connection require TLS (version 0.1.6, pymssql 2.4.3 or
later; earlier pymssql ignores the request and connects in plaintext, so it is refused). The
server certificate is not validated: `SQL_TRUST_SERVER_CERTIFICATE=false` is refused because
the driver cannot validate it. The default `SQL_ENCRYPT=false` is unchanged and logs
`sql.unencrypted` once per process.

Build from the admin repository with `poetry --directory packages/shared build --output ../../dist`. The root lockfile
pins the build's dependency set; consuming applications pin the published wheel and its hash.
