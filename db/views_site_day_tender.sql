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
       SUM(CASE WHEN code = 'IKGIFT' AND net_with_tax < 0 THEN -net_with_tax ELSE 0 END) AS gift_sold
FROM v_fact_payments
GROUP BY business_location_id, business_date, tender_group;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_ro') THEN
        GRANT SELECT ON v_site_day_tender TO dashboard_ro;
    END IF;
END $$;
