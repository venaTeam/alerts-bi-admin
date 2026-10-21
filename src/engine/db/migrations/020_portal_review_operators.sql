-- portal_reviews names the operators each published week was run with.
--
-- The team page shows the team's operators (design "Team operators on the team page",
-- 2026-10-21). The portal reads only portal_* views, so the operators come from the run's own
-- frozen registry entry, runs.registry_entry_snapshot, not the current registry: a reader
-- sees the list that explains the alerts of that week.
--
-- v1_operators is the JSON array text of the entry's v1_operators (JSON_QUERY keeps the values
-- exact, case variants and repeats included). v2_operator is the entry's v2 operator. Both are
-- NULL when the snapshot is not JSON; v1_operators is '[]' when the team listed none.
--
-- The view is the 008 definition verbatim with the two columns appended. Earlier columns keep
-- their positions, no view selects portal_reviews with *, and CREATE OR ALTER keeps existing
-- grants. No row changes.

CREATE OR ALTER VIEW portal_reviews AS
SELECT
    p.publication_id,
    p.team_id,
    r.team_display_name,
    p.run_id,
    p.window_start,
    p.window_end,
    p.published_at,
    p.review_note,
    r.phase_derived,
    r.phase2_readiness_pct,
    CASE
        WHEN LAG(r.ruleset_version) OVER (PARTITION BY p.team_id ORDER BY p.window_end) IS NULL
            THEN CAST(0 AS BIT)
        WHEN LAG(r.ruleset_version) OVER (PARTITION BY p.team_id ORDER BY p.window_end)
                 <> r.ruleset_version
          OR LAG(b.measurement_basis) OVER (PARTITION BY p.team_id ORDER BY p.window_end)
                 <> b.measurement_basis
            THEN CAST(1 AS BIT)
        ELSE CAST(0 AS BIT)
    END AS basis_changed,
    CASE WHEN ISJSON(r.registry_entry_snapshot) = 1
         THEN TRY_CONVERT(DECIMAL(6, 2),
                          JSON_VALUE(r.registry_entry_snapshot, '$.planning.v1_rule_effort_days'))
    END AS v1_rule_effort_days,
    CASE WHEN ISJSON(r.registry_entry_snapshot) = 1
         THEN JSON_QUERY(r.registry_entry_snapshot, '$.v1_operators')
    END AS v1_operators,
    CASE WHEN ISJSON(r.registry_entry_snapshot) = 1
         THEN JSON_VALUE(r.registry_entry_snapshot, '$.v2_operator')
    END AS v2_operator
FROM review_publications AS p
JOIN runs AS r ON r.run_id = p.run_id
CROSS APPLY (
    SELECT HASHBYTES('SHA2_256', CASE
        WHEN ISJSON(r.registry_entry_snapshot) = 1 THEN CONCAT(
            COALESCE(JSON_QUERY(r.registry_entry_snapshot, '$.v1_operators'), N'-'), N'|',
            COALESCE(JSON_QUERY(r.registry_entry_snapshot, '$.panels'), N'-'), N'|',
            COALESCE(N'"' + JSON_VALUE(r.registry_entry_snapshot, '$.v2_operator'), N'-'))
        ELSE N'-|-|-'
    END) AS measurement_basis
) AS b
WHERE p.withdrawn_at IS NULL
  AND r.status = 'completed';
GO
