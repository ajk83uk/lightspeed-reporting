-- Enrichment + categorisation views.
-- These are plain views (no date params). Metabase applies date/site filters
-- on top of them (see metabase/ranking_queries.sql).

BEGIN;

-- ---------------------------------------------------------------------------
-- Wet/dry tag per sales line (by accounting group).
-- A line gets exactly one wet_dry value; unmatched groups -> 'other'.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_line_wet_dry AS
SELECT
    sl.business_location_id,
    sl.account_reference,
    sl.line_id,
    COALESCE(r.category, 'other') AS wet_dry
FROM sales_lines sl
LEFT JOIN category_rules r
       ON r.dimension  = 'wet_dry'
      AND r.active
      AND r.match_type = 'accounting_group'
      AND r.match_value = sl.accounting_group_name;

-- ---------------------------------------------------------------------------
-- Item-category tags per sales line (poppadoms / cocktails / desserts / ...).
-- One row per (line, category). A line normally matches 0 or 1 categories.
-- Supports accounting_group, exact sku, and name ILIKE rules.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_line_item_category AS
SELECT DISTINCT
    sl.business_location_id,
    sl.account_reference,
    sl.line_id,
    r.category
FROM sales_lines sl
JOIN category_rules r
  ON r.dimension = 'item_category'
 AND r.active
 AND (
        (r.match_type = 'accounting_group' AND r.match_value = sl.accounting_group_name)
     OR (r.match_type = 'sku'              AND r.match_value = sl.sku)
     OR (r.match_type = 'name_like'        AND sl.name ILIKE r.match_value)
     );

-- ---------------------------------------------------------------------------
-- Fact view: every sales line enriched with site, date and wet/dry tag.
-- Excludes cancelled receipts. net_ex_vat is the headline sales measure.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_fact_lines AS
SELECT
    sl.business_location_id,
    site.nickname                      AS site,
    sl.account_reference,
    sl.line_id,
    sl.sku,
    sl.name,
    sl.quantity,
    sl.net_without_tax                 AS net_ex_vat,
    sl.net_with_tax                    AS net_inc_vat,
    sl.unit_cost_price,
    sl.accounting_group_name,
    wd.wet_dry,
    sa.time_closed,
    (sa.time_closed AT TIME ZONE 'Europe/London')::date AS business_date,
    -- Trading shift, based on when the line was ordered (UK time):
    --   Lunch 12:00-16:59, Dinner 17:00-21:59, else Other.
    CASE
        WHEN EXTRACT(HOUR FROM (COALESCE(sl.time_of_sale, sa.time_closed) AT TIME ZONE 'Europe/London')) BETWEEN 12 AND 16 THEN 'Lunch (12-5)'
        WHEN EXTRACT(HOUR FROM (COALESCE(sl.time_of_sale, sa.time_closed) AT TIME ZONE 'Europe/London')) BETWEEN 17 AND 21 THEN 'Dinner (5-10)'
        ELSE 'Other'
    END AS shift
FROM sales_lines sl
JOIN sales sa
      ON sa.business_location_id = sl.business_location_id
     AND sa.account_reference    = sl.account_reference
LEFT JOIN sites site
      ON site.business_location_id = sl.business_location_id
LEFT JOIN v_line_wet_dry wd
      ON wd.business_location_id = sl.business_location_id
     AND wd.account_reference    = sl.account_reference
     AND wd.line_id              = sl.line_id
WHERE COALESCE(sa.cancelled, FALSE) = FALSE;

-- ---------------------------------------------------------------------------
-- Fact view: payments enriched with site and date (tips live here).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_fact_payments AS
SELECT
    p.business_location_id,
    site.nickname                      AS site,
    p.account_reference,
    p.payment_uuid,
    p.net_with_tax,
    p.tip,
    p.surcharge,
    sa.time_closed,
    (sa.time_closed AT TIME ZONE 'Europe/London')::date AS business_date,
    CASE
        WHEN EXTRACT(HOUR FROM (sa.time_closed AT TIME ZONE 'Europe/London')) BETWEEN 12 AND 16 THEN 'Lunch (12-5)'
        WHEN EXTRACT(HOUR FROM (sa.time_closed AT TIME ZONE 'Europe/London')) BETWEEN 17 AND 21 THEN 'Dinner (5-10)'
        ELSE 'Other'
    END AS shift
FROM payments p
JOIN sales sa
      ON sa.business_location_id = p.business_location_id
     AND sa.account_reference    = p.account_reference
LEFT JOIN sites site
      ON site.business_location_id = p.business_location_id
WHERE COALESCE(sa.cancelled, FALSE) = FALSE;

-- ---------------------------------------------------------------------------
-- Single flat reporting view for the dashboard: every (non-cancelled) sales
-- line enriched with site, date, shift, wet/dry AND its item_category
-- (poppadoms / cocktails / desserts / NULL). One row per line, one table —
-- so Metabase field filters (date / site / shift) map cleanly to one place.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_report_lines AS
SELECT fl.*,
       (SELECT ic.category
          FROM v_line_item_category ic
         WHERE ic.business_location_id = fl.business_location_id
           AND ic.account_reference    = fl.account_reference
           AND ic.line_id              = fl.line_id
         ORDER BY ic.category
         LIMIT 1) AS item_category
FROM v_fact_lines fl;

-- ---------------------------------------------------------------------------
-- Dedicated VOIDS view: one row per voided (reversal) line. Single table, so
-- Metabase field filters (date / site / shift / staff) map cleanly. Values are
-- negated so voided_value / voided_qty read as positive amounts.
-- staff_name needs the sales pull to include 'staff' (LS_SALES_INCLUDE).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_void_lines AS
SELECT
    sl.business_location_id,
    site.nickname                       AS site,
    sl.account_reference,
    sl.line_id,
    sl.sku,
    sl.name,
    sl.raw->>'voidReason'               AS void_reason,
    COALESCE(NULLIF(sl.raw->>'staffName',''), '(unknown)') AS staff_name,
    -sl.net_without_tax                 AS voided_value,
    -sl.quantity                        AS voided_qty,
    (sa.time_closed AT TIME ZONE 'Europe/London')::date AS business_date,
    CASE
        WHEN EXTRACT(HOUR FROM (COALESCE(sl.time_of_sale, sa.time_closed) AT TIME ZONE 'Europe/London')) BETWEEN 12 AND 16 THEN 'Lunch (12-5)'
        WHEN EXTRACT(HOUR FROM (COALESCE(sl.time_of_sale, sa.time_closed) AT TIME ZONE 'Europe/London')) BETWEEN 17 AND 21 THEN 'Dinner (5-10)'
        ELSE 'Other'
    END                                 AS shift
FROM sales_lines sl
JOIN sales sa
      ON sa.business_location_id = sl.business_location_id
     AND sa.account_reference    = sl.account_reference
LEFT JOIN sites site
      ON site.business_location_id = sl.business_location_id
WHERE COALESCE(sa.cancelled, FALSE) = FALSE
  AND COALESCE(sl.raw->>'voidReason', '') <> '';

-- ---------------------------------------------------------------------------
-- Unified LEAKAGE view: voided lines AND discounted lines in one table, tagged
-- by leakage_type ('Void' / 'Discount'). Single relation -> Metabase field
-- filters (date / site / staff / shift / type) map cleanly. `amount` is the
-- positive money lost (void value or discount given).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_leakage_lines AS
SELECT
    sl.business_location_id,
    site.nickname AS site,
    sl.account_reference, sl.line_id, sl.name,
    'Void'::text  AS leakage_type,
    sl.raw->>'voidReason' AS reason,
    COALESCE(NULLIF(sl.raw->>'staffName',''), '(unknown)') AS staff_name,
    -sl.net_without_tax AS amount,
    (sa.time_closed AT TIME ZONE 'Europe/London')::date AS business_date,
    CASE
        WHEN EXTRACT(HOUR FROM (COALESCE(sl.time_of_sale, sa.time_closed) AT TIME ZONE 'Europe/London')) BETWEEN 12 AND 16 THEN 'Lunch (12-5)'
        WHEN EXTRACT(HOUR FROM (COALESCE(sl.time_of_sale, sa.time_closed) AT TIME ZONE 'Europe/London')) BETWEEN 17 AND 21 THEN 'Dinner (5-10)'
        ELSE 'Other' END AS shift,
    COALESCE(sl.time_of_sale, sa.time_closed) AS tx_time
FROM sales_lines sl
JOIN sales sa ON sa.business_location_id = sl.business_location_id AND sa.account_reference = sl.account_reference
LEFT JOIN sites site ON site.business_location_id = sl.business_location_id
WHERE COALESCE(sa.cancelled, FALSE) = FALSE AND COALESCE(sl.raw->>'voidReason','') <> ''
UNION ALL
SELECT
    sl.business_location_id,
    site.nickname,
    sl.account_reference, sl.line_id, sl.name,
    'Discount'::text,
    COALESCE(NULLIF(sl.raw->>'discountName',''), '(unnamed)'),
    COALESCE(NULLIF(sl.raw->>'staffName',''), '(unknown)'),
    sl.discount_amount,
    (sa.time_closed AT TIME ZONE 'Europe/London')::date,
    CASE
        WHEN EXTRACT(HOUR FROM (COALESCE(sl.time_of_sale, sa.time_closed) AT TIME ZONE 'Europe/London')) BETWEEN 12 AND 16 THEN 'Lunch (12-5)'
        WHEN EXTRACT(HOUR FROM (COALESCE(sl.time_of_sale, sa.time_closed) AT TIME ZONE 'Europe/London')) BETWEEN 17 AND 21 THEN 'Dinner (5-10)'
        ELSE 'Other' END,
    COALESCE(sl.time_of_sale, sa.time_closed)
FROM sales_lines sl
JOIN sales sa ON sa.business_location_id = sl.business_location_id AND sa.account_reference = sl.account_reference
LEFT JOIN sites site ON site.business_location_id = sl.business_location_id
WHERE COALESCE(sa.cancelled, FALSE) = FALSE AND COALESCE(sl.discount_amount, 0) <> 0;

COMMIT;
