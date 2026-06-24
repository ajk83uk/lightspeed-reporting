-- Employee of the Week views (Metabase dashboard 265).
-- Key idea: credit each person for what they ACTUALLY RANG (line-level staffName),
-- not the table opener (the opener differs from the ringer on ~38% of lines).
-- Table-level metrics (covers, tips) that can't be split per line are attributed
-- to the receipt's "primary server" = whoever rang the most value on that check.
-- Re-runnable. Drop order matters (eotw depends on line_staff).

BEGIN;
DROP VIEW IF EXISTS v_staff_eotw_day;
DROP VIEW IF EXISTS v_line_staff;

-- Every report line + who actually rang it.
CREATE VIEW v_line_staff AS
SELECT rl.business_location_id, rl.site, rl.account_reference, rl.line_id,
  rl.business_date, rl.item_category, rl.wet_dry, rl.quantity, rl.net_ex_vat,
  NULLIF(sl.raw->>'staffName','') AS line_staff
FROM v_report_lines rl
JOIN sales_lines sl ON sl.business_location_id=rl.business_location_id
  AND sl.account_reference=rl.account_reference AND sl.line_id=rl.line_id;

-- Per (site, server, day): line-level sales/upsell/wet (by ringer), primary-server
-- covers/sales/tips (dominant ringer per receipt), voids (by ringer), clocked hours.
CREATE VIEW v_staff_eotw_day AS
WITH ls AS (
  SELECT business_location_id bl, site, line_staff staff, business_date,
    SUM(net_ex_vat) line_sales,
    SUM(net_ex_vat) FILTER (WHERE wet_dry='wet') wet_sales,
    SUM(quantity) FILTER (WHERE item_category IN ('desserts','cocktails','241 cocktails','loaded chips','croquettes','poppadoms')) upsell_items
  FROM v_line_staff WHERE line_staff IS NOT NULL GROUP BY 1,2,3,4 ),
ring AS (SELECT business_location_id bl, account_reference ar, line_staff, SUM(net_ex_vat) v FROM v_line_staff WHERE line_staff IS NOT NULL GROUP BY 1,2,3),
prim AS (SELECT bl, ar, line_staff, ROW_NUMBER() OVER (PARTITION BY bl,ar ORDER BY v DESC, line_staff) rn FROM ring),
recagg AS (SELECT bl, ar, SUM(v) tot FROM ring GROUP BY 1,2),
pday AS (
  SELECT p.line_staff staff, s.business_location_id bl,
    (s.time_closed AT TIME ZONE 'Europe/London')::date business_date,
    SUM(COALESCE(s.nb_covers,0)) p_covers, SUM(ra.tot) p_sales,
    SUM(COALESCE((SELECT SUM(tip) FROM payments py WHERE py.business_location_id=s.business_location_id AND py.account_reference=s.account_reference),0)) p_tips
  FROM prim p JOIN sales s ON s.business_location_id=p.bl AND s.account_reference=p.ar
  JOIN recagg ra ON ra.bl=p.bl AND ra.ar=p.ar WHERE p.rn=1 GROUP BY 1,2,3 ),
vd AS (SELECT business_location_id bl, staff_name staff, business_date, SUM(amount) void_value FROM v_leakage_lines WHERE leakage_type='Void' GROUP BY 1,2,3),
hrs AS (SELECT business_location_id bl, staff, business_date, SUM(hours) hours FROM v_staff_hours_day GROUP BY 1,2,3),
spine AS (SELECT DISTINCT bl, staff, business_date FROM (
  SELECT bl,staff,business_date FROM ls
  UNION SELECT bl,staff,business_date FROM pday
  UNION SELECT bl,staff,business_date FROM vd
  UNION SELECT bl,staff,business_date FROM hrs) u)
SELECT sp.bl business_location_id, st.nickname site, sp.staff, sp.business_date,
  COALESCE(ls.line_sales,0) line_sales, COALESCE(ls.wet_sales,0) wet_sales, COALESCE(ls.upsell_items,0) upsell_items,
  COALESCE(pday.p_covers,0) p_covers, COALESCE(pday.p_sales,0) p_sales, COALESCE(pday.p_tips,0) p_tips,
  COALESCE(vd.void_value,0) void_value, COALESCE(hrs.hours,0) hours
FROM spine sp
LEFT JOIN ls   ON ls.bl=sp.bl AND ls.staff=sp.staff AND ls.business_date=sp.business_date
LEFT JOIN pday ON pday.bl=sp.bl AND pday.staff=sp.staff AND pday.business_date=sp.business_date
LEFT JOIN vd   ON vd.bl=sp.bl AND vd.staff=sp.staff AND vd.business_date=sp.business_date
LEFT JOIN hrs  ON hrs.bl=sp.bl AND hrs.staff=sp.staff AND hrs.business_date=sp.business_date
LEFT JOIN sites st ON st.business_location_id=sp.bl;

COMMIT;
