-- StoreKit online sales sourced DIRECTLY from Lightspeed (not the webhook feed).
--
-- Why this exists: StoreKit orders land in the Lightspeed `sales`/`payments`
-- tables via the Order Anywhere integration, so StoreKit sales can be read
-- straight from Lightspeed. Verified: daily gross here reconciles to
-- v_storekit_orders_daily (the Svix webhook feed) to the £ on ~85% of
-- site/days, the rest within VAT-rounding pennies.
--
-- The catch: `owner_name` CANNOT isolate StoreKit. StoreKit is tagged
-- inconsistently across sites (`Online Order` at Bournemouth/Peterborough,
-- `Order Anywhere` at Solihull/Southampton/Portsmouth) AND both those owner
-- labels are shared with on-premise BEER-GARDEN QR at-table ordering (also a
-- Lightspeed Order Anywhere product). That QR revenue is dine-in, not online.
--
-- The reliable discriminator is `table_name`:
--   * on-premise beer-garden / at-table QR -> physical table ref, contains 'Table'
--       e.g. 'Lower Deck Beer Garden, Table 111', 'Upper Deck, Table 37', 'Bar, Table 100'
--   * StoreKit COLLECTION -> 'Order 8G1J'            (order code; no 'Table')
--   * StoreKit DELIVERY   -> 'Michael P. BH4 9JW'    (customer name + UK postcode; no 'Table')
-- So: StoreKit = online owner AND table_name NOT ILIKE '%Table%'.
--
-- Money: payments.net_with_tax is GBP inc-VAT. gross_sales excludes the
-- IKCLIFT paid-out code (mirrors card 198's Total treatment). net ex-VAT /1.20.

DROP VIEW IF EXISTS v_storekit_ls_daily, v_storekit_ls CASCADE;

-- ---------------------------------------------------------------------------
-- One row per StoreKit check: resolved site, order date, channel, gross.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_storekit_ls AS
SELECT
    s.business_location_id,
    regexp_replace(rtrim(st.nickname,'.'),'^Tap ','')            AS site,
    s.account_reference,
    (s.time_closed AT TIME ZONE 'Europe/London')::date           AS order_date,
    s.table_name,
    -- Delivery orders carry a name + UK postcode; collection orders are 'Order XXXX'.
    CASE
        WHEN s.table_name ~* '[A-Z]{1,2}[0-9][0-9A-Z]? *[0-9][A-Z]{2}$' THEN 'Delivery'
        ELSE 'Collection'
    END                                                          AS channel,
    SUM(p.net_with_tax) FILTER (WHERE COALESCE(p.code,'') <> 'IKCLIFT') AS gross_sales
FROM sales s
JOIN payments p
  ON p.business_location_id = s.business_location_id
 AND p.account_reference    = s.account_reference
JOIN sites st ON st.business_location_id = s.business_location_id
WHERE COALESCE(s.cancelled,false) = false
  AND s.owner_name IN ('Order Anywhere','Online Order')  -- the two online owner labels
  AND s.table_name NOT ILIKE '%Table%'                   -- exclude beer-garden / at-table QR
GROUP BY 1,2,3,4,5,6;

-- ---------------------------------------------------------------------------
-- Daily rollup: per site per day. Orders, gross, ex-VAT, AOV, channel split.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_storekit_ls_daily AS
SELECT
    business_location_id,
    site,
    order_date,
    COUNT(*)                                             AS orders,
    SUM(gross_sales)                                     AS gross_sales,
    SUM(gross_sales) / 1.20                              AS net_sales_ex_vat,
    AVG(gross_sales)                                     AS aov_gross,
    COUNT(*) FILTER (WHERE channel = 'Collection')       AS collection_orders,
    COUNT(*) FILTER (WHERE channel = 'Delivery')         AS delivery_orders,
    SUM(gross_sales) FILTER (WHERE channel = 'Collection') AS collection_gross,
    SUM(gross_sales) FILTER (WHERE channel = 'Delivery')   AS delivery_gross
FROM v_storekit_ls
GROUP BY 1,2,3;
