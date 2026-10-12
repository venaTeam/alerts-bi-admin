-- basis_changed follows the team's own measurement, not the registry file's version.
--
-- Migration 005 compared runs.registry_version, which is one version for the whole file and
-- is bumped whenever any team is enrolled (design section 7.11). Every onboarding therefore
-- restarted every team's time-to-v2 lookback. A week now counts as measured differently only
-- when its ruleset changed, or when the team's own stored entry changed what is measured:
-- its v1 operators, its v2 operator or its panels (SQL and variables). Its display name,
-- planning override and weekly enrolment are not measurement.
--
-- The three values are hashed together with SHA2_256 so the comparison is exact: no
-- case-insensitive collation and no trailing-space padding can hide a changed operator. The
-- two arrays are JSON_QUERY fragments (self-delimiting); the v2 operator is prefixed with a
-- quote so a null is never confused with a value. src.admin.summary.measurement_basis
-- computes the same rule for the operator app.
--
-- The view is the 005 definition verbatim except for the basis_changed CASE and the
-- CROSS APPLY that names the hashed basis. Its columns are unchanged, so the views built on
-- it are unaffected, and CREATE OR ALTER keeps existing grants.

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
    END AS v1_rule_effort_days
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
