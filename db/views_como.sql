-- Como loyalty reporting views.
-- Money: tables hold PENCE; views expose £. Como TotalSum is assumed GROSS
-- (inc VAT) -- confirm on a live sale vs the Lightspeed check -- so an ex-VAT
-- figure (/1.20) is provided for like-for-like with Lightspeed sales_exvat.
-- Dates are Europe/London. Site resolves via como_site_map; an unmapped
-- BranchID shows as 'Unmapped (<id>)' rather than disappearing.

DROP VIEW IF EXISTS v_como_capture_daily, v_como_redemptions_daily, v_como_daily,
                    v_como_signups_weekly, v_como_signups_daily, v_como_tx CASCADE;

-- One clean row per loyalty purchase.
CREATE OR REPLACE VIEW v_como_tx AS
SELECT
    t.purchase_uid,
    t.membership_key,
    t.branch_id,
    COALESCE(m.site_name, 'Unmapped (' || COALESCE(t.branch_id,'?') || ')') AS site,
    m.business_location_id,
    t.transaction_id,
    t.purchased_at,
    (t.purchased_at AT TIME ZONE 'Europe/London')::date AS business_date,
    t.total_pence / 100.0                AS gross_total,
    t.total_pence / 100.0 / 1.20         AS net_exvat,
    t.num_members
FROM como_transactions t
LEFT JOIN como_site_map m ON m.como_branch_id = t.branch_id;

-- Sign-ups per day / week (estate-wide: the joinedclub payload carries no
-- reliable branch, so per-site sign-ups are attributed by FIRST PURCHASE site
-- in v_como_signups_daily.first_purchase_site; members who have not yet
-- bought show as 'No purchase yet').
CREATE OR REPLACE VIEW v_como_signups_daily AS
SELECT
    (COALESCE(mem.registered_at, mem.joined_event_at) AT TIME ZONE 'Europe/London')::date AS signup_date,
    COALESCE(sm.site_name, CASE WHEN mem.first_branch_id IS NULL THEN 'No purchase yet'
                                ELSE 'Unmapped (' || mem.first_branch_id || ')' END) AS first_purchase_site,
    COUNT(*) AS signups,
    COUNT(*) FILTER (WHERE mem.allow_sms)   AS sms_optin,
    COUNT(*) FILTER (WHERE mem.allow_email) AS email_optin
FROM como_members mem
LEFT JOIN como_site_map sm ON sm.como_branch_id = mem.first_branch_id
WHERE COALESCE(mem.registered_at, mem.joined_event_at) IS NOT NULL
GROUP BY 1, 2;

-- Weekly estate sign-ups with week-on-week movement (your original ask).
CREATE OR REPLACE VIEW v_como_signups_weekly AS
WITH w AS (
    SELECT date_trunc('week', signup_date)::date AS week_start, SUM(signups) AS signups
      FROM v_como_signups_daily GROUP BY 1)
SELECT week_start,
       signups,
       LAG(signups) OVER (ORDER BY week_start)                         AS prev_week,
       signups - LAG(signups) OVER (ORDER BY week_start)               AS change,
       ROUND(100.0 * (signups - LAG(signups) OVER (ORDER BY week_start))
             / NULLIF(LAG(signups) OVER (ORDER BY week_start), 0), 1)  AS change_pct
  FROM w;

-- Daily loyalty sales per site.
CREATE OR REPLACE VIEW v_como_daily AS
SELECT site, business_location_id, business_date,
       COUNT(*)                         AS loyalty_checks,
       COUNT(DISTINCT membership_key)   AS active_members,
       SUM(gross_total)                 AS loyalty_gross,
       SUM(net_exvat)                   AS loyalty_net_exvat,
       ROUND(AVG(gross_total), 2)       AS avg_check_gross
  FROM v_como_tx
 GROUP BY site, business_location_id, business_date;

-- Redemption cost per day (value of benefits given away).
CREATE OR REPLACE VIEW v_como_redemptions_daily AS
SELECT COALESCE(m.site_name, 'Unmapped (' || COALESCE(r.branch_id,'?') || ')') AS site,
       (r.redeemed_at AT TIME ZONE 'Europe/London')::date AS business_date,
       r.asset_type,
       COUNT(*)                          AS redemptions,
       SUM(r.applied_pence) / 100.0      AS value_given
  FROM como_redemptions r
  LEFT JOIN como_site_map m ON m.como_branch_id = r.branch_id
 GROUP BY 1, 2, 3;

-- Capture rate: loyalty-identified net sales / Lightspeed net sales (ex VAT).
-- Needs business_location_id on the site map to join; rows without it are
-- excluded (they would be Unmapped anyway).
CREATE OR REPLACE VIEW v_como_capture_daily AS
SELECT d.site, d.business_location_id, d.business_date,
       d.loyalty_checks, d.active_members, d.loyalty_net_exvat,
       s.sales_exvat                      AS lightspeed_net_exvat,
       ROUND(100.0 * d.loyalty_net_exvat / NULLIF(s.sales_exvat, 0), 1) AS capture_pct
  FROM v_como_daily d
  JOIN v_site_day s
    ON s.business_location_id = d.business_location_id
   AND s.business_date = d.business_date;
