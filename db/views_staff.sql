-- Staff performance view: per site, per server (receipt owner), per day.
-- Powers the Staff Performance dashboard (upselling, tips, productivity).
-- Leakage (voids/discounts) by staff comes from v_leakage_lines (line-level
-- staff_name), so it's not duplicated here.
--
-- "staff" = the receipt owner (the server who owns the check). Online orders
-- ('Order Anywhere') and 'Head Office' logins are excluded as non-servers.
-- Covers come from the receipt; note cover-based ratios (APC, per-cover rates)
-- are unreliable for bar-led staff who don't record covers on drinks tabs.
--
-- Re-runnable. Depends on db/views.sql (v_report_lines) + db/schema.sql.

BEGIN;

CREATE OR REPLACE VIEW v_staff_day AS
WITH rec AS (   -- one row per receipt: owner, covers, tips
  SELECT s.business_location_id, st.nickname AS site, s.owner_name AS staff,
    (s.time_closed AT TIME ZONE 'Europe/London')::date AS business_date,
    s.account_reference, COALESCE(s.nb_covers,0) AS covers,
    COALESCE((SELECT SUM(p.tip) FROM payments p
              WHERE p.business_location_id=s.business_location_id
                AND p.account_reference=s.account_reference),0) AS tips
  FROM sales s JOIN sites st ON st.business_location_id=s.business_location_id
  WHERE COALESCE(s.cancelled,false)=false
    AND COALESCE(s.owner_name,'') NOT IN ('Order Anywhere','Head Office')
),
lines AS (      -- per receipt: sales + upsell item counts
  SELECT rl.business_location_id, rl.account_reference,
    SUM(rl.net_ex_vat) AS sales_exvat,
    SUM(rl.quantity) FILTER(WHERE rl.item_category='desserts') AS dessert_qty,
    SUM(rl.quantity) FILTER(WHERE rl.item_category IN ('cocktails','241 cocktails')) AS cocktail_qty,
    SUM(rl.quantity) FILTER(WHERE rl.item_category IN ('loaded chips','croquettes')) AS special_qty,
    SUM(rl.quantity) FILTER(WHERE rl.wet_dry='wet') AS drink_qty
  FROM v_report_lines rl GROUP BY 1,2
)
SELECT rec.business_location_id, rec.site, rec.staff, rec.business_date,
  COUNT(*) AS receipts, SUM(rec.covers) AS covers, SUM(rec.tips) AS tips,
  SUM(COALESCE(l.sales_exvat,0)) AS sales_exvat,
  SUM(COALESCE(l.dessert_qty,0)) AS dessert_qty,
  SUM(COALESCE(l.cocktail_qty,0)) AS cocktail_qty,
  SUM(COALESCE(l.special_qty,0)) AS special_qty,
  SUM(COALESCE(l.drink_qty,0)) AS drink_qty
FROM rec LEFT JOIN lines l
  ON l.business_location_id=rec.business_location_id AND l.account_reference=rec.account_reference
GROUP BY 1,2,3,4;

-- staff_id -> server name (from sales owner + line staff; one name per id).
CREATE OR REPLACE VIEW v_staff_names AS
SELECT staff_id, MIN(name) AS name FROM (
    SELECT (raw->>'ownerId')::bigint AS staff_id, owner_name AS name
      FROM sales WHERE raw->>'ownerId' ~ '^[0-9]+$' AND owner_name IS NOT NULL
    UNION ALL
    SELECT (raw->>'staffId')::bigint, raw->>'staffName'
      FROM sales_lines WHERE raw->>'staffId' ~ '^[0-9]+$' AND NULLIF(raw->>'staffName','') IS NOT NULL
) z GROUP BY staff_id;

-- Clocked hours per site, per server, per day (from staff_shifts). Open shifts
-- (no CLOCK_OUT) contribute no hours. Join to v_staff_day on (site,staff,date).
CREATE OR REPLACE VIEW v_staff_hours_day AS
SELECT ss.business_location_id, st.nickname AS site, ss.staff_id,
       COALESCE(nm.name, '#'||ss.staff_id) AS staff,
       (ss.clock_in AT TIME ZONE 'Europe/London')::date AS business_date,
       SUM(EXTRACT(epoch FROM (ss.clock_out - ss.clock_in))/3600.0)
         FILTER (WHERE ss.clock_out IS NOT NULL) AS hours
FROM staff_shifts ss
JOIN sites st ON st.business_location_id = ss.business_location_id
LEFT JOIN v_staff_names nm ON nm.staff_id = ss.staff_id
WHERE ss.clock_in IS NOT NULL
GROUP BY 1,2,3,4,5;

COMMIT;
