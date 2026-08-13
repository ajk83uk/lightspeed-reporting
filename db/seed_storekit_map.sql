-- One-off seed for storekit_site_map (venue.id -> reporting site).
-- IDs supplied by StoreKit (Thomas Metral, 2026-07-13). Safe to re-run.
INSERT INTO storekit_site_map (venue_id, site_name) VALUES
    (12397, 'Solihull'),
    (12394, 'Bournemouth'),
    (12395, 'Portsmouth'),
    (12398, 'Peterborough'),
    (12396, 'Southampton')
ON CONFLICT (venue_id) DO UPDATE SET
    site_name = EXCLUDED.site_name, updated_at = now();

-- Resolve business_location_id from the master sites dimension by name match
-- (sites.nickname is the POS label, e.g. "Tap Solihull"). Safe to re-run.
UPDATE storekit_site_map m
   SET business_location_id = s.business_location_id, updated_at = now()
  FROM sites s
 WHERE s.nickname ILIKE '%' || m.site_name || '%'
   AND (m.business_location_id IS DISTINCT FROM s.business_location_id);
