# Console log format — accepted 2026-10-20

The product owner reported on 2026-10-20 that the services' and workers' logs did not explain
what was happening and left out details an investigation needs. Shown a proposed console
format, they approved it the same day with one change: the message comes before the bracketed
team, run and job identifiers. They did not object to the defaults proposed with it: console
output by default with JSON available, successful page reads at debug, and an idle heartbeat
every 15 minutes. See design section "Console log format" for the authoritative behavior.

Consequences accepted with the decision: the pods no longer write JSON by default, so anything
that parses their output needs `LOG_FORMAT=json`; every record goes to stderr; and the admin
console's successful page reads leave no record unless `LOG_LEVEL=debug`.

What may be logged does not change: identifiers, hashes, counts, timings and redacted errors,
never alert documents, model payloads or credentials. Shared 0.1.9 holds the format and the
request logging; operations 0.2.8 pins it. Admin adopts both and the portal vendors the shared
wheel. Merge design, admin and portal together; deploy only with separate authorization.
