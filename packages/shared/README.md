# Alerts BI shared library

Versioned Python values, SQL connection configuration, summary calculations and pure HTML
presentation used by the admin and portal applications. It has no application imports, registry or
operator write operations. SQL consumers supply the existing `SQL_*` configuration.

`SQL_ENCRYPT=true` makes every connection require TLS (version 0.1.7, pymssql 2.4.3 or
later; earlier pymssql ignores the request and connects in plaintext, so it is refused).
`SQL_ENCRYPT` accepts only 1/true/yes/on or 0/false/no/off; anything else fails rather than
meaning off. The server certificate is not validated, so TLS stops passive reading but not
an active interceptor; `SQL_TRUST_SERVER_CERTIFICATE=false` is refused because the driver
cannot validate it. Web pools refuse unenforceable settings when constructed (app startup);
dedicated connections refuse them before connecting. The default `SQL_ENCRYPT=false` is
unchanged and logs `sql.unencrypted` once per process for each SQL server (host and port).

Build from the admin repository with `poetry --directory packages/shared build --output ../../dist`. The root lockfile
pins the build's dependency set; consuming applications pin the published wheel and its hash.
