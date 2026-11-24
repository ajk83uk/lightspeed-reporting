-- Daily totals per site and tender, for the tapapp cash-off "Pull sales data now" button.
-- Aggregates only (no receipt or payment ids), so the read-only login dashboard_ro can be given
-- this one view without opening v_fact_payments. Safe to re-run.
CREATE OR REPLACE VIEW v_site_day_tender AS
SELECT business_location_id,
       business_date,
       tender_group,
       SUM(net_with_tax)        AS net_with_tax,
       SUM(COALESCE(tip, 0))    AS tip,
       MAX(time_closed)         AS latest_closed,
       COUNT(*)                 AS payments,
       -- IKGIFT: positive = voucher redeemed against a bill, negative = voucher sold
       SUM(CASE WHEN code = 'IKGIFT' AND net_with_tax > 0 THEN net_with_tax ELSE 0 END)  AS gift_redeemed,
       SUM(CASE WHEN code = 'IKGIFT' AND net_with_tax < 0 THEN -net_with_tax ELSE 0 END) AS gift_sold,
       -- STR = Storekit / Order Anywhere online orders, with any tip on them (matches the Solihull and Peterborough forms)
       SUM(CASE WHEN code = 'STR' THEN net_with_tax + COALESCE(tip, 0) ELSE 0 END)        AS online_orders
FROM v_fact_payments
GROUP BY business_location_id, business_date, tender_group;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_ro') THEN
        GRANT SELECT ON v_site_day_tender TO dashboard_ro;
    END IF;
END $$;

-- 15-minute totals per site and day (London time), for the tapapp owner's live sales graph. Aggregates only.
CREATE OR REPLACE VIEW v_site_day_slots AS
SELECT business_location_id,
       business_date,
       (EXTRACT(HOUR FROM time_closed AT TIME ZONE 'Europe/London') * 60
        + FLOOR(EXTRACT(MINUTE FROM time_closed AT TIME ZONE 'Europe/London') / 15) * 15)::int AS slot_min,
       SUM(net_with_tax)       AS net_with_tax,
       SUM(COALESCE(tip, 0))   AS tip,
       COUNT(*)                AS payments
FROM v_fact_payments
WHERE time_closed IS NOT NULL
GROUP BY business_location_id, business_date, 3;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_ro') THEN
        GRANT SELECT ON v_site_day_slots TO dashboard_ro;
    END IF;
END $$;
