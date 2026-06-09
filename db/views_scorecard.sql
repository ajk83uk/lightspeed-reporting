-- Scorecard views: per-site-per-day and per-site-per-week metrics + group ranks.
-- These power the five site scorecards and the two management dashboards.
-- NO money is exposed downstream: sales/delivery surface only as a RANK (1-5);
-- £ totals live here purely to drive ranking and the void %.
--
-- Grain:
--   v_site_day         one row per (site, trading day) with all raw measures
--   v_site_day_ranked  + group rank per metric for that day
--   v_site_week        same, aggregated to the ISO week (Mon-start)
--   v_site_week_ranked + group rank per metric for that week
--
-- Re-runnable (CREATE OR REPLACE). Depends on db/views.sql + db/schema.sql.

BEGIN;

-- ---------------------------------------------------------------------------
-- Per site, per day.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_site_day AS
WITH sites_norm AS (
    SELECT business_location_id,
           nickname AS site,
           -- normalise "Tap Bournemouth." / "Solihull" -> "bournemouth"/"solihull"
           replace(regexp_replace(lower(nickname), '[^a-z]', '', 'g'), 'tap', '') AS site_key
    FROM sites
),
lines_agg AS (   -- everything derivable from sales lines
    SELECT business_location_id,
           business_date,
           SUM(net_ex_vat)                                            AS sales_exvat,
           SUM(quantity) FILTER (WHERE item_category = 'poppadoms')     AS poppadom_qty,
           SUM(quantity) FILTER (WHERE item_category = '241 cocktails') AS cocktail241_qty,
           SUM(quantity) FILTER (WHERE item_category = 'desserts')      AS dessert_qty,
           SUM(net_ex_vat) FILTER (WHERE wet_dry = 'wet')              AS wet_exvat,
           SUM(net_ex_vat) FILTER (WHERE wet_dry = 'dry')              AS dry_exvat,
           SUM(quantity)   FILTER (WHERE wet_dry = 'wet')             AS drink_qty,
           SUM(quantity)   FILTER (WHERE wet_dry = 'dry')             AS plate_qty
    FROM v_report_lines
    GROUP BY business_location_id, business_date
),
covers_agg AS (  -- covers live on the receipt, not the line
    SELECT sa.business_location_id,
           (sa.time_closed AT TIME ZONE 'Europe/London')::date AS business_date,
           SUM(COALESCE(sa.nb_covers, 0)) AS covers
    FROM sales sa
    WHERE COALESCE(sa.cancelled, FALSE) = FALSE
    GROUP BY 1, 2
),
void_agg AS (
    SELECT business_location_id, business_date, SUM(voided_value) AS void_value
    FROM v_void_lines
    GROUP BY 1, 2
),
delivery_agg AS (  -- delivery £ from the cash-off sheets, mapped to the POS site
    SELECT sn.business_location_id,
           co.business_date,
           SUM(COALESCE(co.uber_eats,0) + COALESCE(co.just_eat,0) + COALESCE(co.deliveroo,0)) AS delivery_total
    FROM cashoff_daily co
    JOIN sites_norm sn
      ON sn.site_key = replace(regexp_replace(lower(co.site), '[^a-z]', '', 'g'), 'tap', '')
    GROUP BY 1, 2
),
base AS (  -- one row per (site, day) that appears in ANY source
    SELECT business_location_id, business_date FROM lines_agg
    UNION SELECT business_location_id, business_date FROM covers_agg
    UNION SELECT business_location_id, business_date FROM void_agg
    UNION SELECT business_location_id, business_date FROM delivery_agg
)
SELECT
    b.business_location_id,
    sn.site,
    b.business_date,
    -- traded? (had any sales lines that day)
    (la.sales_exvat IS NOT NULL)                               AS traded,
    la.sales_exvat,
    da.delivery_total,
    -- item counts: 0 if the site traded but sold none; NULL if no POS data
    CASE WHEN la.sales_exvat IS NOT NULL THEN COALESCE(la.poppadom_qty,0)    END AS poppadom_qty,
    CASE WHEN la.sales_exvat IS NOT NULL THEN COALESCE(la.cocktail241_qty,0) END AS cocktail241_qty,
    CASE WHEN la.sales_exvat IS NOT NULL THEN COALESCE(la.dessert_qty,0)     END AS dessert_qty,
    CASE WHEN la.sales_exvat IS NOT NULL THEN COALESCE(va.void_value,0)      END AS void_value,
    CASE WHEN COALESCE(la.sales_exvat,0) > 0
         THEN ROUND((COALESCE(va.void_value,0) / la.sales_exvat * 100)::numeric, 2)
    END AS void_pct,
    la.wet_exvat,
    la.dry_exvat,
    CASE WHEN COALESCE(la.wet_exvat,0) + COALESCE(la.dry_exvat,0) > 0
         THEN ROUND((COALESCE(la.wet_exvat,0) / (COALESCE(la.wet_exvat,0)+COALESCE(la.dry_exvat,0)) * 100)::numeric, 1)
    END AS wet_pct,
    CASE WHEN COALESCE(la.wet_exvat,0) + COALESCE(la.dry_exvat,0) > 0
         THEN ROUND((COALESCE(la.dry_exvat,0) / (COALESCE(la.wet_exvat,0)+COALESCE(la.dry_exvat,0)) * 100)::numeric, 1)
    END AS dry_pct,
    ca.covers,
    CASE WHEN la.sales_exvat IS NOT NULL THEN COALESCE(la.drink_qty,0) END AS drink_qty,
    CASE WHEN la.sales_exvat IS NOT NULL THEN COALESCE(la.plate_qty,0) END AS plate_qty,
    CASE WHEN COALESCE(ca.covers,0) > 0
         THEN ROUND((COALESCE(la.drink_qty,0)::numeric / ca.covers), 2)
    END AS drinks_per_cover
FROM base b
JOIN sites_norm sn   ON sn.business_location_id = b.business_location_id
LEFT JOIN lines_agg    la ON la.business_location_id = b.business_location_id AND la.business_date = b.business_date
LEFT JOIN covers_agg   ca ON ca.business_location_id = b.business_location_id AND ca.business_date = b.business_date
LEFT JOIN void_agg     va ON va.business_location_id = b.business_location_id AND va.business_date = b.business_date
LEFT JOIN delivery_agg da ON da.business_location_id = b.business_location_id AND da.business_date = b.business_date;

-- ---------------------------------------------------------------------------
-- Per site, per day, WITH group ranks (1 = best). Rank is NULL where the site
-- has no value for that metric, so non-reporting sites show blank not a fake 5.
-- More-is-better metrics rank DESC; void % ranks ASC (lower = better).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_site_day_ranked AS
SELECT d.*,
    CASE WHEN sales_exvat     IS NOT NULL THEN RANK() OVER (PARTITION BY business_date ORDER BY sales_exvat     DESC NULLS LAST) END AS sales_rank,
    CASE WHEN delivery_total  IS NOT NULL THEN RANK() OVER (PARTITION BY business_date ORDER BY delivery_total  DESC NULLS LAST) END AS delivery_rank,
    CASE WHEN poppadom_qty    IS NOT NULL THEN RANK() OVER (PARTITION BY business_date ORDER BY poppadom_qty    DESC NULLS LAST) END AS poppadom_rank,
    CASE WHEN cocktail241_qty IS NOT NULL THEN RANK() OVER (PARTITION BY business_date ORDER BY cocktail241_qty DESC NULLS LAST) END AS cocktail241_rank,
    CASE WHEN dessert_qty     IS NOT NULL THEN RANK() OVER (PARTITION BY business_date ORDER BY dessert_qty     DESC NULLS LAST) END AS dessert_rank,
    CASE WHEN void_pct        IS NOT NULL THEN RANK() OVER (PARTITION BY business_date ORDER BY void_pct        ASC NULLS LAST)  END AS void_rank
FROM v_site_day d;

-- ---------------------------------------------------------------------------
-- Per site, per ISO week (Monday start). Percentages/ratios are recomputed
-- from weekly sums (never averaged).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_site_week AS
SELECT
    business_location_id,
    site,
    date_trunc('week', business_date)::date AS week_start,
    SUM(sales_exvat)      AS sales_exvat,
    SUM(delivery_total)   AS delivery_total,
    SUM(poppadom_qty)     AS poppadom_qty,
    SUM(cocktail241_qty)  AS cocktail241_qty,
    SUM(dessert_qty)      AS dessert_qty,
    SUM(void_value)       AS void_value,
    CASE WHEN SUM(sales_exvat) > 0
         THEN ROUND((SUM(COALESCE(void_value,0)) / SUM(sales_exvat) * 100)::numeric, 2) END AS void_pct,
    SUM(wet_exvat)        AS wet_exvat,
    SUM(dry_exvat)        AS dry_exvat,
    CASE WHEN SUM(COALESCE(wet_exvat,0)) + SUM(COALESCE(dry_exvat,0)) > 0
         THEN ROUND((SUM(COALESCE(wet_exvat,0)) / (SUM(COALESCE(wet_exvat,0))+SUM(COALESCE(dry_exvat,0))) * 100)::numeric, 1) END AS wet_pct,
    CASE WHEN SUM(COALESCE(wet_exvat,0)) + SUM(COALESCE(dry_exvat,0)) > 0
         THEN ROUND((SUM(COALESCE(dry_exvat,0)) / (SUM(COALESCE(wet_exvat,0))+SUM(COALESCE(dry_exvat,0))) * 100)::numeric, 1) END AS dry_pct,
    SUM(covers)           AS covers,
    SUM(drink_qty)        AS drink_qty,
    SUM(plate_qty)        AS plate_qty,
    CASE WHEN SUM(COALESCE(covers,0)) > 0
         THEN ROUND((SUM(COALESCE(drink_qty,0))::numeric / SUM(covers)), 2) END AS drinks_per_cover
FROM v_site_day
GROUP BY business_location_id, site, date_trunc('week', business_date)::date;

CREATE OR REPLACE VIEW v_site_week_ranked AS
SELECT w.*,
    CASE WHEN sales_exvat     IS NOT NULL THEN RANK() OVER (PARTITION BY week_start ORDER BY sales_exvat     DESC NULLS LAST) END AS sales_rank,
    CASE WHEN delivery_total  IS NOT NULL THEN RANK() OVER (PARTITION BY week_start ORDER BY delivery_total  DESC NULLS LAST) END AS delivery_rank,
    CASE WHEN poppadom_qty    IS NOT NULL THEN RANK() OVER (PARTITION BY week_start ORDER BY poppadom_qty    DESC NULLS LAST) END AS poppadom_rank,
    CASE WHEN cocktail241_qty IS NOT NULL THEN RANK() OVER (PARTITION BY week_start ORDER BY cocktail241_qty DESC NULLS LAST) END AS cocktail241_rank,
    CASE WHEN dessert_qty     IS NOT NULL THEN RANK() OVER (PARTITION BY week_start ORDER BY dessert_qty     DESC NULLS LAST) END AS dessert_rank,
    CASE WHEN void_pct        IS NOT NULL THEN RANK() OVER (PARTITION BY week_start ORDER BY void_pct        ASC NULLS LAST)  END AS void_rank
FROM v_site_week w;

COMMIT;
