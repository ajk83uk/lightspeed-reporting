-- Kitchen prep planning views (Zindiya).
-- Grain 1: v_kitchen_dish_day  -- units sold per dish per day per channel
--   Restaurant = Lightspeed lines (positive-qty lines only, voids excluded by
--   the negative-reversal netting: we count SUM(quantity) which nets voids).
--   Market     = Peazi product sales at Saint Pauls (Tip lines excluded).
-- Grain 2: v_kitchen_prep_dow  -- per dish x channel x ISO weekday over the
--   last 84 days (12 of each weekday): average units, P80 ("prep to" level so
--   ~4 in 5 days are fully covered), max, and days traded.
-- Re-runnable (CREATE OR REPLACE).

CREATE OR REPLACE VIEW v_kitchen_dish_day AS
SELECT 'Restaurant'::text AS channel,
       business_date,
       name AS dish,
       SUM(quantity)   AS units,
       SUM(net_ex_vat) AS net_value
FROM v_report_lines
WHERE accounting_group_name = 'Food'
GROUP BY 2, 3
HAVING SUM(quantity) > 0
UNION ALL
SELECT 'Market (St Pauls)'::text,
       (order_time AT TIME ZONE 'Europe/London')::date,
       name,
       SUM(quantity),
       SUM(total)
FROM peazi_order_lines
WHERE name IS DISTINCT FROM 'Tip'
GROUP BY 2, 3
HAVING SUM(quantity) > 0;

CREATE OR REPLACE VIEW v_kitchen_prep_dow AS
WITH recent AS (
    SELECT *, EXTRACT(isodow FROM business_date)::int AS dow_iso,
           to_char(business_date, 'Dy') AS dow
    FROM v_kitchen_dish_day
    WHERE business_date BETWEEN current_date - 84 AND current_date - 1
)
SELECT channel, dish, dow_iso, MIN(dow) AS dow,
       COUNT(*)                                        AS days_sold,
       ROUND(AVG(units), 1)                            AS avg_units,
       ROUND(percentile_cont(0.8) WITHIN GROUP (ORDER BY units)::numeric, 1) AS prep_to_p80,
       MAX(units)                                      AS max_units,
       ROUND(SUM(net_value))                           AS net_value_84d
FROM recent
GROUP BY channel, dish, dow_iso;
