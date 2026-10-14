-- One-off seed for como_site_map (Como BranchID -> reporting site).
-- IDs from the Como dashboard (June 2026). The BranchID that arrives in real
-- `purchase` webhooks may differ (Como's docs sample shows a short id like
-- "67") -- after the first live events run:
--   SELECT branch_id, COUNT(*) FROM como_events GROUP BY 1;
-- and add any extra ids below. Safe to re-run.
INSERT INTO como_site_map (como_branch_id, site_name) VALUES
    ('1718940401139714', 'Bournemouth'),
    ('1718940401139719', 'Peterborough'),
    ('1718940401139718', 'Portsmouth'),
    ('1199129099239426', 'Solihull'),
    ('1199129099239431', 'Southampton')
ON CONFLICT (como_branch_id) DO UPDATE SET
    site_name = EXCLUDED.site_name, updated_at = now();

-- Resolve business_location_id from the master sites dimension by name
-- (sites.nickname is the POS label e.g. "Tap Solihull"). Safe to re-run.
UPDATE como_site_map m
   SET business_location_id = s.business_location_id, updated_at = now()
  FROM sites s
 WHERE s.nickname ILIKE '%' || m.site_name || '%'
   AND s.business_location_id <> 1718940401166502          -- 'Locations Manager'
   AND (m.business_location_id IS DISTINCT FROM s.business_location_id);
