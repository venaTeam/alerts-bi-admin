# Operator backfill — accepted 2026-10-14

The product owner approved a multi-week backfill in the admin console after reviewing an
interactive mock, and chose to assess backfilled weeks with the model. See
docs/backfill.md for the authoritative behavior. This reverses the earlier rule that model
coverage never runs backwards and replaces the planned automatic deterministic sweep.

Admin owns the dialog, the queued and cancelled run job states, the worker claim order, the
weekly wait for a running backfill week, the new console routes and migration 014. Portal
views, shared/operations interfaces, CSV outputs and deterministic identifiers are unchanged.
Merge design and admin together; deploy only with separate authorization. Rolling the app back
leaves the additive revision installed; cancel or finish queued backfill weeks first.
