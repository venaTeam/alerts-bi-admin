# Team operators mock (accepted 2026-10-21)

Accepted 2026-10-21 and implemented (design "Team operators on the team page"): show the
team's operators on the team page in both the admin console and the review portal. Operators are the registry's `v1_operators` and `v2_operator` values
(`contracts/teams.schema.json`) that route alerts to the team. This folder keeps the reviewed
mock; the design section is authoritative.

Open the pages from a local HTTP server, so the stylesheet and script load:

```bash
python -m http.server 8231 --directory docs/mockups/team-operators
```

Then open `http://localhost:8231/admin.html` and `http://localhost:8231/portal.html`. The yellow
bar at the top belongs only to the mock: it switches between four sample teams (v1 only with a
registry change since the run, v2 only, migrating with many operators, no operators).

The stylesheets are frozen copies of admin `fb87c91` (shared `STYLESHEET` + `CONSOLE_CSS`) and
portal `203fd7a` (`STYLESHEET`), so the pages look like the apps did when the mock was reviewed.
They are not kept in sync and will drift from the apps; the live styles are in admin
`src/assets.py` and portal `src/assets.py`. Each page's own `<style>` block holds the rules the
mock proposed. `scripts/check_docs.py` checks that each copy names its source commit and that
every file a page links exists.

## What the mock shows

**Admin console, team page.** An "Operators" panel between the header and the week's
Run/Review/Publish steps:

- v1 operators as exact values (all of them, with a count) and the v2 operator, or "None" /
  "Not set yet".
- The current registry version and effective date.
- A note when the selected week's run froze a different registry version, listing what that run
  used, because the console's list is the current registry.
- A note when the team has no operators (a run finds no alerts).
- A line when an operator is also listed for another team (the schema allows repeats).

**Review portal, team week (Overview tab).** An "Operators" card in the side column, below Phase:

- v1 and v2 operators that this published week was run with (its frozen registry snapshot), not
  the current registry, so the list always explains the alerts on the page.
- The first six values, then "Show N more".
- An empty state when the review listed no operators.
- No explanatory sentence or registry version (removed in review on 2026-10-21).

## Resolved 2026-10-21

1. **Admin source:** the current registry, with a note when the selected run's frozen list differs.
2. **Portal data:** migration `020_portal_review_operators` adds `v1_operators` and `v2_operator`
   to `portal_reviews` from the run's frozen registry entry.
3. **Exposure:** the product owner asked for the portal to show them; the registry version stays
   hidden.
4. **Naming:** "Operators", as mocked.
5. **Placement:** portal Overview only; the admin team cards are unchanged.
