-- ===========================================================================
-- Metabase ranking questions (the 1-5 site league tables).
--
-- HOW TO USE IN METABASE
--   1. New question -> Native query -> pick the Postgres database.
--   2. Paste one query below.
--   3. Metabase auto-detects the {{start}} and {{end}} variables. Set both to
--      type "Date" and mark them as a single "Field filter" date range if you
--      want a date picker, or leave as two date variables.
--   4. Save each as its own question, then add them all to one dashboard.
--   5. On the dashboard add a Date filter and wire it to {{start}}/{{end}} of
--      every card -> one date control drives all six league tables.
--
-- Rank 1 = best (highest) on each metric. Ties share a rank (RANK()).
-- All money is ex-VAT (net_ex_vat). Change to net_inc_vat if you prefer gross.
-- COALESCE(...,0) is used in every ORDER BY so a site with zero of a metric
-- ranks last (Postgres otherwise sorts NULLs first).
-- ===========================================================================

-- 1) TOTAL SALES -------------------------------------------------------------
SELECT
    RANK() OVER (ORDER BY COALESCE(SUM(net_ex_vat), 0) DESC) AS rank,
    site,
    ROUND(COALESCE(SUM(net_ex_vat), 0), 2) AS total_sales
FROM v_fact_lines
WHERE time_closed BETWEEN {{start}} AND {{end}}
GROUP BY site
ORDER BY rank;

-- 2) WET vs DRY --------------------------------------------------------------
-- Ranks sites by WET (drink) sales, and also shows dry sales + wet %.
SELECT
    RANK() OVER (ORDER BY COALESCE(SUM(net_ex_vat) FILTER (WHERE wet_dry = 'wet'), 0) DESC) AS rank_wet,
    site,
    ROUND(COALESCE(SUM(net_ex_vat) FILTER (WHERE wet_dry = 'wet'), 0), 2) AS wet_sales,
    ROUND(COALESCE(SUM(net_ex_vat) FILTER (WHERE wet_dry = 'dry'), 0), 2) AS dry_sales,
    ROUND(
        100.0 * SUM(net_ex_vat) FILTER (WHERE wet_dry = 'wet')
        / NULLIF(SUM(net_ex_vat) FILTER (WHERE wet_dry IN ('wet','dry')), 0)
    , 1) AS wet_pct
FROM v_fact_lines
WHERE time_closed BETWEEN {{start}} AND {{end}}
GROUP BY site
ORDER BY rank_wet;

-- 3) TIPS --------------------------------------------------------------------
SELECT
    RANK() OVER (ORDER BY COALESCE(SUM(tip), 0) DESC) AS rank,
    site,
    ROUND(COALESCE(SUM(tip), 0), 2) AS total_tips
FROM v_fact_payments
WHERE time_closed BETWEEN {{start}} AND {{end}}
GROUP BY site
ORDER BY rank;

-- 4) POPPADOMS ---------------------------------------------------------------
-- #4/#5/#6 are the same query with a different category literal. LEFT JOIN +
-- COALESCE so every site appears and a site that sold none ranks last (not
-- dropped, and not wrongly first via NULL sorting).
SELECT
    RANK() OVER (ORDER BY COALESCE(SUM(fl.net_ex_vat) FILTER (WHERE ic.category = 'poppadoms'), 0) DESC) AS rank,
    fl.site,
    ROUND(COALESCE(SUM(fl.net_ex_vat) FILTER (WHERE ic.category = 'poppadoms'), 0), 2) AS net_sales,
    ROUND(COALESCE(SUM(fl.quantity)   FILTER (WHERE ic.category = 'poppadoms'), 0), 0) AS units
FROM v_fact_lines fl
LEFT JOIN v_line_item_category ic
      ON ic.business_location_id = fl.business_location_id
     AND ic.account_reference    = fl.account_reference
     AND ic.line_id              = fl.line_id
WHERE fl.time_closed BETWEEN {{start}} AND {{end}}
GROUP BY fl.site
ORDER BY rank;

-- 5) COCKTAILS ---------------------------------------------------------------
SELECT
    RANK() OVER (ORDER BY COALESCE(SUM(fl.net_ex_vat) FILTER (WHERE ic.category = 'cocktails'), 0) DESC) AS rank,
    fl.site,
    ROUND(COALESCE(SUM(fl.net_ex_vat) FILTER (WHERE ic.category = 'cocktails'), 0), 2) AS net_sales,
    ROUND(COALESCE(SUM(fl.quantity)   FILTER (WHERE ic.category = 'cocktails'), 0), 0) AS units
FROM v_fact_lines fl
LEFT JOIN v_line_item_category ic
      ON ic.business_location_id = fl.business_location_id
     AND ic.account_reference    = fl.account_reference
     AND ic.line_id              = fl.line_id
WHERE fl.time_closed BETWEEN {{start}} AND {{end}}
GROUP BY fl.site
ORDER BY rank;

-- 6) DESSERTS ----------------------------------------------------------------
SELECT
    RANK() OVER (ORDER BY COALESCE(SUM(fl.net_ex_vat) FILTER (WHERE ic.category = 'desserts'), 0) DESC) AS rank,
    fl.site,
    ROUND(COALESCE(SUM(fl.net_ex_vat) FILTER (WHERE ic.category = 'desserts'), 0), 2) AS net_sales,
    ROUND(COALESCE(SUM(fl.quantity)   FILTER (WHERE ic.category = 'desserts'), 0), 0) AS units
FROM v_fact_lines fl
LEFT JOIN v_line_item_category ic
      ON ic.business_location_id = fl.business_location_id
     AND ic.account_reference    = fl.account_reference
     AND ic.line_id              = fl.line_id
WHERE fl.time_closed BETWEEN {{start}} AND {{end}}
GROUP BY fl.site
ORDER BY rank;

-- ---------------------------------------------------------------------------
-- BONUS: combined league table (one row per site, a column per metric + its
-- rank). Handy as a single "scoreboard" tile.
-- ---------------------------------------------------------------------------
WITH base AS (
    SELECT
        fl.site,
        SUM(fl.net_ex_vat)                                  AS total_sales,
        SUM(fl.net_ex_vat) FILTER (WHERE fl.wet_dry='wet')  AS wet_sales,
        SUM(fl.net_ex_vat) FILTER (WHERE fl.wet_dry='dry')  AS dry_sales,
        SUM(fl.net_ex_vat) FILTER (WHERE ic.category='poppadoms') AS poppadom_sales,
        SUM(fl.net_ex_vat) FILTER (WHERE ic.category='cocktails') AS cocktail_sales,
        SUM(fl.net_ex_vat) FILTER (WHERE ic.category='desserts')  AS dessert_sales
    FROM v_fact_lines fl
    LEFT JOIN v_line_item_category ic
           ON ic.business_location_id = fl.business_location_id
          AND ic.account_reference    = fl.account_reference
          AND ic.line_id              = fl.line_id
    WHERE fl.time_closed BETWEEN {{start}} AND {{end}}
    GROUP BY fl.site
),
tips AS (
    SELECT site, SUM(tip) AS total_tips
    FROM v_fact_payments
    WHERE time_closed BETWEEN {{start}} AND {{end}}
    GROUP BY site
)
SELECT
    b.site,
    ROUND(COALESCE(b.total_sales,0),2) AS total_sales,
    RANK() OVER (ORDER BY COALESCE(b.total_sales,0)    DESC) AS rank_sales,
    RANK() OVER (ORDER BY COALESCE(b.wet_sales,0)      DESC) AS rank_wet,
    RANK() OVER (ORDER BY COALESCE(t.total_tips,0)     DESC) AS rank_tips,
    RANK() OVER (ORDER BY COALESCE(b.poppadom_sales,0) DESC) AS rank_poppadoms,
    RANK() OVER (ORDER BY COALESCE(b.cocktail_sales,0) DESC) AS rank_cocktails,
    RANK() OVER (ORDER BY COALESCE(b.dessert_sales,0)  DESC) AS rank_desserts
FROM base b
LEFT JOIN tips t USING (site)
ORDER BY rank_sales;
