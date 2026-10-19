# Operator console redesign — accepted 2026-10-19

The user reviewed three console mocks on 2026-10-18, chose the guided-steps layout, then asked on
2026-10-19 to merge New run and Backfill weeks into one New runs dialog and to implement it. See
design section "Operator console redesign" for the authoritative behavior. It replaces the
2026-10-08 run history, tabs, split findings panel and publish dialog, and the backfill
contract's calendar dialog and gap banner.

Admin owns the pages, stylesheet, script and the `week` field on `POST /teams/{team_id}/trigger`,
which dispatches one week to the manual job path and two to twelve weeks to the backfill path.
Both admission paths, the backfill route, migrations, portal views, shared/operations libraries,
CSV outputs and deterministic identifiers are unchanged. Merge design and admin together; deploy
only with separate authorization. Rolling the app back restores the previous pages with no data
change.
