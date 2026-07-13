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
    SUM(rl.quantity) FILTER(WHERE rl.item_category IN ('loaded chips','croquettes','railway chicken curry')) AS special_qty,
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

-- Per site/server/day metrics that power the monthly "rank of ranks" staff
-- league table (Metabase card 206). One row per (site, server, day) with every
-- rankable metric: sales/tips/covers/desserts/specials from v_staff_day, plus
-- poppadom + 2-4-1 counts (from report lines), table turn time (dwell = closed -
-- opening), and clocked hours. The league card ranks each server 1..N within
-- their site on each category and sums the positions (lowest = best); "Reviews"
-- from the legacy PDF report is intentionally omitted (no source in Lightspeed).
CREATE OR REPLACE VIEW v_staff_scorecard_day AS
WITH cat AS (
  SELECT rl.business_location_id, rl.site, s.owner_name AS staff, rl.business_date,
    SUM(rl.quantity) FILTER (WHERE rl.item_category='poppadoms')     AS poppadom_qty,
    SUM(rl.quantity) FILTER (WHERE rl.item_category='241 cocktails') AS c241_qty
  FROM v_report_lines rl
  JOIN sales s ON s.business_location_id=rl.business_location_id AND s.account_reference=rl.account_reference
  WHERE COALESCE(s.cancelled,false)=false AND COALESCE(s.owner_name,'') NOT IN ('Order Anywhere','Head Office')
  GROUP BY 1,2,3,4
),
turn AS (
  SELECT s.business_location_id, st.nickname AS site, s.owner_name AS staff,
    (s.time_closed AT TIME ZONE 'Europe/London')::date AS business_date,
    SUM(EXTRACT(epoch FROM (s.time_closed - s.time_opening))/60.0) AS dwell_min,
    COUNT(*) AS turn_receipts
  FROM sales s JOIN sites st ON st.business_location_id=s.business_location_id
  WHERE COALESCE(s.cancelled,false)=false AND s.time_opening IS NOT NULL AND s.time_closed IS NOT NULL
    AND COALESCE(s.owner_name,'') NOT IN ('Order Anywhere','Head Office')
  GROUP BY 1,2,3,4
)
SELECT d.business_location_id, d.site, d.staff, d.business_date,
  d.covers, d.receipts, d.tips, d.sales_exvat, d.dessert_qty, d.special_qty,
  COALESCE(c.poppadom_qty,0)  AS poppadom_qty,
  COALESCE(c.c241_qty,0)      AS c241_qty,
  COALESCE(t.dwell_min,0)     AS dwell_min,
  COALESCE(t.turn_receipts,0) AS turn_receipts,
  COALESCE(h.hours,0)         AS hours
FROM v_staff_day d
LEFT JOIN cat  c ON c.business_location_id=d.business_location_id AND c.staff=d.staff AND c.business_date=d.business_date
LEFT JOIN turn t ON t.business_location_id=d.business_location_id AND t.staff=d.staff AND t.business_date=d.business_date
LEFT JOIN v_staff_hours_day h ON h.business_location_id=d.business_location_id AND h.staff=d.staff AND h.business_date=d.business_date;

-- Per site/staff/month count of 5-star reviews whose text mentions the staff's
-- first name. Replicates Daysi's manual "type each name into 5-star reviews"
-- step for the Monthly Staff Report (Metabase dashboard 595, cards 496/497).
-- Depends on v_sentiment_reviews (views_sentiment.sql runs earlier in migrate).
-- Word-boundary match (\m..\M) avoids substring hits; generic non-name logins
-- are excluded. Approximate by nature: misspelt or unnamed reviews are missed,
-- shared first names are double-counted -- same trade-offs as the manual method.
CREATE OR REPLACE VIEW v_staff_reviews_month AS
WITH staff AS (
  SELECT DISTINCT business_location_id, site, staff,
    regexp_replace(staff,'[^a-zA-Z]','','g') AS nm
  FROM v_staff_scorecard_day
  WHERE staff NOT IN ('Manager','Bar','Floor','Staff','Team','Host','Server',
                      'Kitchen','Chef','Duty','Waiter','Waitress','Admin','Trainee')
),
rev AS (
  SELECT business_location_id, review_month, review_text
  FROM v_sentiment_reviews
  WHERE rating >= 5 AND COALESCE(review_text,'') <> ''
)
SELECT s.business_location_id, s.site, s.staff, r.review_month AS mth,
       COUNT(*) AS reviews
FROM staff s
JOIN rev r
  ON r.business_location_id = s.business_location_id
 AND length(s.nm) >= 3
 AND r.review_text ~* ('\m'||s.nm||'\M')
GROUP BY 1,2,3,4;

COMMIT;
