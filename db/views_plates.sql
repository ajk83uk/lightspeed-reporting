-- Plates per cover = food dishes rung / covers. "Food" = the POS Food accounting
-- group (== wet_dry 'dry'), i.e. ALL food incl. poppadoms (per owner decision).
-- Drinks excluded. Two grains: site/day and primary-server/day.
-- NOTE: the Last Night dashboard's per-server card uses a date-filtered inline
-- query (filters to current_date-1 BEFORE the heavy join) for speed; this view
-- is the reusable full-history version for period analysis.

-- Shareable items count as 2 dishes (owner decision 2026-06-24): Mixed Grill,
-- Vegetarian Grill, Vegan Mixed Grill (all "* grill" variants) and Poppadoms.
-- The LIVE Metabase cards (346/347 Last Night, 348/349 Weekly) carry this same
-- weighting inline; these views mirror it for the site grain.

-- SITE x DAY: receipt-level plates + covers, bucketed by the receipt's London date.
CREATE OR REPLACE VIEW v_plates_per_cover_site AS
WITH recf AS (
  SELECT business_location_id bl, account_reference ar,
         SUM(CASE WHEN accounting_group_name='Food'
                  THEN quantity * (CASE WHEN lower(name) ~ 'mixed grill|vegetarian grill|poppad' THEN 2 ELSE 1 END)
                  ELSE 0 END) plates
  FROM sales_lines GROUP BY 1,2)
SELECT regexp_replace(rtrim(st.nickname,'.'),'^Tap ','') site,
       s.business_location_id,
       (s.time_closed AT TIME ZONE 'Europe/London')::date business_date,
       SUM(recf.plates)                                   plates,
       SUM(COALESCE(s.nb_covers,0))                       covers,
       ROUND(SUM(recf.plates)/NULLIF(SUM(s.nb_covers),0),2) plates_per_cover
FROM sales s
JOIN recf ON recf.bl=s.business_location_id AND recf.ar=s.account_reference
LEFT JOIN sites st ON st.business_location_id=s.business_location_id
WHERE COALESCE(s.cancelled,false)=false AND s.time_closed IS NOT NULL
GROUP BY 1,2,3;

-- PRIMARY SERVER x DAY: each receipt's plates + covers credited to its dominant
-- ringer (same primary-server rule as Employee of the Week).
CREATE OR REPLACE VIEW v_plates_per_cover_staff AS
WITH ring AS (
  SELECT business_location_id bl, account_reference ar, line_staff,
         SUM(net_ex_vat) v FROM v_line_staff WHERE line_staff IS NOT NULL GROUP BY 1,2,3),
prim AS (
  SELECT bl, ar, line_staff, ROW_NUMBER() OVER (PARTITION BY bl,ar ORDER BY v DESC, line_staff) rn FROM ring),
recf AS (
  SELECT business_location_id bl, account_reference ar,
         SUM(quantity) FILTER (WHERE wet_dry='dry') plates FROM v_line_staff GROUP BY 1,2)
SELECT p.line_staff staff,
       s.business_location_id,
       regexp_replace(rtrim(st.nickname,'.'),'^Tap ','') site,
       (s.time_closed AT TIME ZONE 'Europe/London')::date business_date,
       SUM(recf.plates)                                   plates,
       SUM(COALESCE(s.nb_covers,0))                       covers,
       ROUND(SUM(recf.plates)/NULLIF(SUM(s.nb_covers),0),2) plates_per_cover
FROM prim p
JOIN sales s   ON s.business_location_id=p.bl AND s.account_reference=p.ar
JOIN recf      ON recf.bl=p.bl AND recf.ar=p.ar
LEFT JOIN sites st ON st.business_location_id=p.bl
WHERE p.rn=1 AND COALESCE(s.cancelled,false)=false AND s.time_closed IS NOT NULL
  AND p.line_staff NOT IN ('Order Anywhere','Head Office','Online Order')
GROUP BY 1,2,3,4;
