-- StoreKit online-order reporting views.
-- Grain: per site per day (and per ISO week). Site label resolves via
-- storekit_site_map (venue_id -> reporting name); if the map is not yet seeded
-- we fall back to the venue.name carried on each order row. Cross-source joins
-- use business_location_id (the master key) once the map is filled.
--
-- Money: storekit_orders holds PENCE; views expose £ (pence/100.0). total is
-- gross (inc VAT); a derived ex-VAT figure is provided for like-for-like with
-- the Lightspeed headline (which ranks on net_ex_vat). VAT assumed 20%.

DROP VIEW IF EXISTS v_storekit_orders_weekly, v_storekit_orders_daily, v_storekit_orders CASCADE;

-- ---------------------------------------------------------------------------
-- One clean row per order: resolved site label + £ amounts + validity flag.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_storekit_orders AS
SELECT
    o.order_id,
    o.venue_id,
    COALESCE(m.site_name, o.venue_name)            AS site,
    m.business_location_id,
    o.code,
    o.order_type,
    -- normalise channel: anything not pickup/delivery is treated as dine-in
    CASE
        WHEN o.order_type ILIKE '%deliver%' THEN 'Delivery'
        WHEN o.order_type ILIKE '%pickup%' OR o.order_type ILIKE '%collect%' THEN 'Pickup'
        ELSE 'Dine-in'
    END                                            AS channel,
    o.status,
    o.is_refunded,
    -- A "valid" (revenue-counting) order: not rejected/canceled.
    (o.status NOT IN ('rejected', 'canceled'))     AS is_valid,
    o.created_at,
    (o.created_at AT TIME ZONE 'Europe/London')::date           AS order_date,
    date_trunc('week', (o.created_at AT TIME ZONE 'Europe/London'))::date AS order_week,
    o.total_pence       / 100.0                    AS gross_total,
    o.tip_pence         / 100.0                    AS tip,
    o.delivery_fee_pence/ 100.0                    AS delivery_fee,
    o.discount_pence    / 100.0                    AS discount,
    o.net_sales_pence   / 100.0                    AS net_sales,        -- total - tip - delivery
    COALESCE(o.refund_total_pence,0) / 100.0       AS refund,
    o.covers,
    o.customer_email,
    o.marketing_consent
FROM storekit_orders o
LEFT JOIN storekit_site_map m ON m.venue_id = o.venue_id;

-- ---------------------------------------------------------------------------
-- Daily: per site per day. Order count, sales, AOV, tips, discounts, refunds,
-- plus a pickup/delivery/dine-in split. Excludes rejected/canceled.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_storekit_orders_daily AS
SELECT
    site,
    business_location_id,
    order_date,
    COUNT(*)                                              AS orders,
    SUM(net_sales)                                        AS net_sales,
    SUM(gross_total)                                      AS gross_sales,
    SUM(net_sales) / 1.20                                 AS net_sales_ex_vat,
    AVG(net_sales)                                        AS aov,            -- average net order value
    SUM(tip)                                              AS tips,
    SUM(delivery_fee)                                     AS delivery_fees,
    SUM(discount)                                         AS discounts,
    SUM(refund)                                           AS refunds,
    -- channel split (counts)
    COUNT(*) FILTER (WHERE channel = 'Pickup')           AS pickup_orders,
    COUNT(*) FILTER (WHERE channel = 'Delivery')         AS delivery_orders,
    COUNT(*) FILTER (WHERE channel = 'Dine-in')          AS dinein_orders,
    -- channel split (net sales)
    SUM(net_sales) FILTER (WHERE channel = 'Pickup')     AS pickup_net_sales,
    SUM(net_sales) FILTER (WHERE channel = 'Delivery')   AS delivery_net_sales,
    SUM(net_sales) FILTER (WHERE channel = 'Dine-in')    AS dinein_net_sales
FROM v_storekit_orders
WHERE is_valid
GROUP BY site, business_location_id, order_date;

-- ---------------------------------------------------------------------------
-- Weekly rollup (ISO week starting Monday), same metric set.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_storekit_orders_weekly AS
SELECT
    site,
    business_location_id,
    order_week,
    COUNT(*)                                              AS orders,
    SUM(net_sales)                                        AS net_sales,
    SUM(gross_total)                                      AS gross_sales,
    SUM(net_sales) / 1.20                                 AS net_sales_ex_vat,
    AVG(net_sales)                                        AS aov,
    SUM(tip)                                              AS tips,
    SUM(delivery_fee)                                     AS delivery_fees,
    SUM(discount)                                         AS discounts,
    SUM(refund)                                           AS refunds,
    COUNT(*) FILTER (WHERE channel = 'Pickup')           AS pickup_orders,
    COUNT(*) FILTER (WHERE channel = 'Delivery')         AS delivery_orders,
    COUNT(*) FILTER (WHERE channel = 'Dine-in')          AS dinein_orders
FROM v_storekit_orders
WHERE is_valid
GROUP BY site, business_location_id, order_week;
