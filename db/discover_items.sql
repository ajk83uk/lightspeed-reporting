-- Helper queries to validate / tune the category rules against the live
-- catalogue. Run these after the first ingestion, eyeball the output, then
-- tighten seed_categories.sql (prefer explicit SKUs over name patterns once
-- you know what's there).

-- 1) Which catalogue items currently match each item_category rule?
SELECT r.category, r.match_type, r.match_value,
       i.business_location_id, i.sku, i.name, i.accounting_group_name
FROM items i
JOIN category_rules r
  ON r.dimension = 'item_category'
 AND r.active
 AND (
        (r.match_type = 'accounting_group' AND r.match_value = i.accounting_group_name)
     OR (r.match_type = 'sku'              AND r.match_value = i.sku)
     OR (r.match_type = 'name_like'        AND i.name ILIKE r.match_value)
     )
ORDER BY r.category, i.name;

-- 2) Distinct item names within an accounting group (to find what to map).
--    Change the group name as needed.
SELECT DISTINCT accounting_group_name, name, sku
FROM items
WHERE accounting_group_name = 'Food'
ORDER BY name;

-- 3) Sold items NOT yet captured by any item_category rule (last 90 days),
--    ranked by revenue -- shows what big sellers you might be missing.
SELECT fl.accounting_group_name, fl.name,
       SUM(fl.net_ex_vat) AS net_sales,
       SUM(fl.quantity)   AS qty
FROM v_fact_lines fl
LEFT JOIN v_line_item_category ic
       ON ic.business_location_id = fl.business_location_id
      AND ic.account_reference    = fl.account_reference
      AND ic.line_id              = fl.line_id
WHERE ic.category IS NULL
  AND fl.time_closed >= now() - INTERVAL '90 days'
GROUP BY fl.accounting_group_name, fl.name
ORDER BY net_sales DESC
LIMIT 100;
