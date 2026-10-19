# Admin console without login — accepted 2026-10-19

The product owner asked on 2026-10-19 to remove the login to the admin UI entirely. Asked what
should replace it, they chose: record no operator names at all, erase the names already stored,
keep Delete run in the console, and let everyone reach the console because only their team will
have the URL. See design section "Admin console without login" for the authoritative behavior.

Consequences accepted with the decision: anyone with the URL can run, publish, withdraw, decide
and permanently delete runs; no record says who did any of it; and migration 018 permanently
blanks the stored names, recoverable only from a pre-018 backup.

Admin owns the console, CLI, migration 018 and operations 0.2.7. The portal stops showing the
decision maker and needs no new shared library. Deployment drops the oauth-proxy, its Role and
RoleBinding, TLS and cookie secrets and the Route allowlist. Merge design, admin and portal
together; deploy only with separate authorization and through the migration gate.
