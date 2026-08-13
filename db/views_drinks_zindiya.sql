-- =====================================================================
-- Zindiya Drinks Ordering  (mirror of Tap & Tandoor v_drinks_* chain)
-- Single site: Zindiya (business_location_id 1199146279108610)
-- Window: last 7 completed days, Europe/London.
-- Rules confirmed with Ajay (2026-07-13):
--   * Draught = Cobra + Jute only, 30L kegs. All other beers/ciders are
--     packaged singles (counted as units).
--   * Single spirit = 25ml, double = 50ml. IGNORE the GBP0 "base" spirit
--     SKUs; only count the explicit 25ml/50ml measure SKUs.
--   * Cocktail spirit usage from the May-2025 batching specs.
--     (Spritz recipes not in the spec sheet are best-guess single measures.)
-- =====================================================================

-- ---------- BASE: drinks sold in the last 7 days -----------------------
CREATE OR REPLACE VIEW v_zindiya_drinks_base AS
SELECT 'Zindiya'::text AS site, sku, name, accounting_group_name, menu_list_price,
       sum(quantity) AS qty
FROM sales_lines sl
WHERE quantity > 0
  AND accounting_group_name IN ('Alcoholic Drinks','Non-Alcoholic Drinks')
  AND time_of_sale >= ((date_trunc('day', now() AT TIME ZONE 'Europe/London') - interval '7 days') AT TIME ZONE 'Europe/London')
  AND time_of_sale <  ((date_trunc('day', now() AT TIME ZONE 'Europe/London')) AT TIME ZONE 'Europe/London')
GROUP BY sku, name, accounting_group_name, menu_list_price;

-- ---------- DRAUGHT KEGS (Cobra + Jute, 30L) ---------------------------
CREATE OR REPLACE VIEW v_zindiya_drinks_kegs AS
WITH keg_ref(sku, keg_name, keg_litres, serve_ml) AS (
  VALUES
    ('636','Cobra',30,568),   -- Cobra Pint
    ('635','Cobra',30,284),   -- Half Cobra
    ('7119','Cobra',30,284),  -- Cobra Shandy (~half beer)
    ('591','Jute IPA',30,568),-- Jute IPA Pint
    ('588','Jute IPA',30,284) -- Half Jute IPA
)
SELECT b.site, k.keg_name, k.keg_litres AS keg_size_l,
       sum(b.qty) AS pints_halves_sold,
       round(sum(b.qty * k.serve_ml) / 1000.0, 1) AS litres_sold,
       round(sum(b.qty * k.serve_ml) / 568.0, 1) AS pints_sold,
       round(sum(b.qty * k.serve_ml) / (k.keg_litres * 1000.0), 2) AS kegs_consumed,
       ceil(sum(b.qty * k.serve_ml) / (k.keg_litres * 1000.0)) AS kegs_to_order
FROM v_zindiya_drinks_base b
JOIN keg_ref k ON k.sku = b.sku
GROUP BY b.site, k.keg_name, k.keg_litres
ORDER BY litres_sold DESC;

-- ---------- PACKAGED BEER / CIDER (bottles & cans, count units) --------
CREATE OR REPLACE VIEW v_zindiya_drinks_packaged AS
WITH pkg_ref(sku, product) AS (
  VALUES
    ('699','White Rhino Lager'),
    ('572','Thornbridge Jaipur IPA'),
    ('615','Pulp Mango & Lime Cider'),
    ('510','Salt Jute Session IPA 330ml'),
    ('513','Salt Jute Shandy')
)
SELECT b.site, p.product,
       sum(b.qty) AS units_sold,
       ceil(sum(b.qty)) AS units_to_order
FROM v_zindiya_drinks_base b
JOIN pkg_ref p ON p.sku = b.sku
GROUP BY b.site, p.product
ORDER BY units_sold DESC;

-- ---------- SPIRITS (straight measures + cocktail BOM) -----------------
-- Straight spirits detected DYNAMICALLY from the 25ml/50ml modifier line names
-- (Lightspeed rings the measure into the product name). Auto-captures new
-- spirits without maintaining a SKU list. Excludes 0% spirits and £0 base lines.
CREATE OR REPLACE VIEW v_zindiya_drinks_spirits AS
WITH straight_ml AS (
  SELECT b.site,
         trim(regexp_replace(b.name, '\s*(25|50)ml.*$', '')) AS spirit,
         sum(b.qty * CASE WHEN b.name ~ '(^| )50ml' THEN 50 ELSE 25 END) AS ml
  FROM v_zindiya_drinks_base b
  WHERE b.accounting_group_name = 'Alcoholic Drinks'
    AND b.name ~ '(^| )(25|50)ml($| )'
    AND b.name !~ '0%'
  GROUP BY b.site, trim(regexp_replace(b.name, '\s*(25|50)ml.*$', ''))
),
cocktail_bom(sku, spirit, ml) AS (
  VALUES
    -- Chai Negroni (542 / 542.)
    ('542','Campari',25),('542','Crazy Punjabi Chai Gin',25),('542','Martini Rosso',25),
    ('542.','Campari',25),('542.','Crazy Punjabi Chai Gin',25),('542.','Martini Rosso',25),
    -- Chai Wala (551 / 551.)  (Prosecco omitted)
    ('551','Crazy Punjabi Chai Gin',20),('551.','Crazy Punjabi Chai Gin',20),
    -- Indian Summer Spritz (566 / 566.)
    ('566','Tanqueray Rangpur',18.75),('566','Ginger Liqueur',6.25),('566','Aperol',20),
    ('566.','Tanqueray Rangpur',18.75),('566.','Ginger Liqueur',6.25),('566.','Aperol',20),
    -- Crazy Martini (465)
    ('465','Crazy Lassi Gin',25),('465','Ume Sake',17.5),('465','Lychee Liqueur',17.5),
    -- Indian Rose (571 / 571.)
    ('571','Cazcabel Honey Tequila',20),('571','Velvet Falernum',20),('571','Lanique Rose Vodka',20),
    ('571.','Cazcabel Honey Tequila',20),('571.','Velvet Falernum',20),('571.','Lanique Rose Vodka',20),
    -- King Louie (582)
    ('582','Banana Liqueur',30),('582','Captain Morgan',30),
    -- Mango Margarita (554 / 554.)
    ('554','Casamigos Blanco',42.86),('554','Ginger Liqueur',16.07),
    ('554.','Casamigos Blanco',42.86),('554.','Ginger Liqueur',16.07),
    -- Rakshasa (618 / 618.)
    ('618','Tequila',40),('618','Koko Kanu',20),
    ('618.','Tequila',40),('618.','Koko Kanu',20),
    -- Spice Route (702)
    ('702','Woodford Reserve',37.5),('702','Koko Kanu',10.71),('702','Glenfiddich 12',5.36),
    -- The Secret Garden (547)
    ('547','Chase Rhubarb Vodka',24.62),('547','Chambord',15.38),
    -- Tagore''s Tipple (555)
    ('555','Casamigos Reposado',25),('555','Baileys',50),
    -- Paan Colada (553 / 553.)
    ('553','Captain Morgan White',50),('553','Sambuca',10),
    ('553.','Captain Morgan White',50),('553.','Sambuca',10),
    -- Aperol Spritz (598 / 598.)  [50ml spirit + 100ml Prosecco, per Ajay]
    ('598','Aperol',50),('598.','Aperol',50),
    -- Campari Spritz (472)  [assumption]
    ('472','Campari',50),
    -- Sarti Spritz (471 / 471.)  [assumption]
    ('471','Sarti',50),('471.','Sarti',50),
    -- Hugo Spritz (517 / 517.)  [assumption]
    ('517','St Germain Elderflower',50),('517.','St Germain Elderflower',50)
    -- Madhuri (469) & Goan Sunrise (467): no spirit in spec
),
cocktail_ml AS (
  SELECT b.site, x.spirit, sum(b.qty * x.ml) AS ml
  FROM v_zindiya_drinks_base b JOIN cocktail_bom x ON x.sku = b.sku
  GROUP BY b.site, x.spirit
),
combined AS (
  SELECT site, spirit, ml, 'straight'::text AS src FROM straight_ml
  UNION ALL
  SELECT site, spirit, ml, 'cocktail'::text AS src FROM cocktail_ml
)
SELECT site, spirit,
       COALESCE(sum(ml) FILTER (WHERE src='straight'),0) AS straight_ml,
       COALESCE(sum(ml) FILTER (WHERE src='cocktail'),0) AS cocktail_ml,
       sum(ml) AS total_ml,
       round(sum(ml)/700.0,2) AS bottles_700ml,
       -- Only order a bottle if >50% of it was consumed: whole bottles used
       -- plus one more only when the leftover exceeds half a 700ml bottle.
       floor(sum(ml)/700.0) + CASE WHEN (sum(ml) - 700*floor(sum(ml)/700.0)) > 350 THEN 1 ELSE 0 END AS bottles_to_order
FROM combined
GROUP BY site, spirit
ORDER BY total_ml DESC;

-- ---------- WINE -------------------------------------------------------
CREATE OR REPLACE VIEW v_zindiya_drinks_wine AS
WITH wine_ref(sku, wine_name, ml) AS (
  VALUES
    ('100','Pinot Grigio',175),('100.','Pinot Grigio',175),('101','Pinot Grigio',250),('102','Pinot Grigio',750),
    ('609','Pinot Grigio Blush',175),('610','Pinot Grigio Blush',250),('611','Pinot Grigio Blush',750),
    ('94','Sauvignon Blanc',175),('95','Sauvignon Blanc',250),('96','Sauvignon Blanc',750),
    ('92','Prosecco',125),('92.','Prosecco',125),('93','Prosecco',750),
    ('629','Rioja',175),('630','Rioja',250),('631','Rioja',750),
    ('493','Malbec',175),('494','Malbec',250),('436','Malbec',750),
    ('612','Pinot Noir',175),('613','Pinot Noir',250),('614','Pinot Noir',750),
    ('646','Chardonnay',175),('647','Chardonnay',250),('698','Chardonnay',750),
    ('531','Zinfandel',175),('532','Zinfandel',250)
),
by_glass AS (
  SELECT b.site, w.wine_name, sum(b.qty * w.ml) AS ml
  FROM v_zindiya_drinks_base b JOIN wine_ref w ON w.sku = b.sku
  GROUP BY b.site, w.wine_name
),
cocktail_prosecco AS (
  -- Sarti / Hugo / Aperol spritzes each use 100ml Prosecco (per Ajay)
  SELECT b.site, 'Prosecco'::text AS wine_name, sum(b.qty * 100) AS ml
  FROM v_zindiya_drinks_base b
  WHERE b.sku IN ('471','471.','517','517.','598','598.')
  GROUP BY b.site
),
allw AS (SELECT site,wine_name,ml FROM by_glass UNION ALL SELECT site,wine_name,ml FROM cocktail_prosecco)
SELECT site, wine_name,
       sum(ml) AS ml_poured,
       round(sum(ml)/750.0,2) AS bottles_equiv,
       ceil(sum(ml)/750.0) AS bottles_to_order
FROM allw
GROUP BY site, wine_name
ORDER BY ml_poured DESC;

-- ---------- COCKTAILS (serves) -----------------------------------------
CREATE OR REPLACE VIEW v_zindiya_drinks_cocktails AS
WITH cocktail_ref(sku, cocktail_name) AS (
  VALUES
    ('554','Mango Margarita'),('554.','Mango Margarita'),
    ('467','Goan Sunrise'),
    ('618','Rakshasa'),('618.','Rakshasa'),
    ('566','Indian Summer Spritz'),('566.','Indian Summer Spritz'),
    ('551','Chai Wala'),('551.','Chai Wala'),
    ('465','Crazy Martini'),
    ('553','Paan Colada'),('553.','Paan Colada'),
    ('469','Madhuri'),
    ('571','Indian Rose'),('571.','Indian Rose'),
    ('702','Spice Route'),
    ('582','King Louie'),
    ('542','Chai Negroni'),('542.','Chai Negroni'),
    ('547','The Secret Garden'),
    ('555','Tagore''s Tipple'),
    ('471','Sarti Spritz'),('471.','Sarti Spritz'),
    ('517','Hugo Spritz'),('517.','Hugo Spritz'),
    ('598','Aperol Spritz'),('598.','Aperol Spritz'),
    ('472','Campari Spritz')
)
SELECT b.site, c.cocktail_name,
       sum(CASE WHEN b.menu_list_price > 0 THEN b.qty ELSE 0 END) AS paid_serves,
       sum(CASE WHEN b.menu_list_price = 0 THEN b.qty ELSE 0 END) AS bottomless_serves,
       sum(b.qty) AS total_serves
FROM v_zindiya_drinks_base b JOIN cocktail_ref c ON c.sku = b.sku
GROUP BY b.site, c.cocktail_name
ORDER BY total_serves DESC;

-- ---------- SOFT DRINKS ------------------------------------------------
CREATE OR REPLACE VIEW v_zindiya_drinks_softs AS
SELECT b.site, b.name AS product,
       sum(CASE WHEN b.menu_list_price > 0 THEN b.qty ELSE 0 END) AS paid_units,
       sum(CASE WHEN b.menu_list_price = 0 THEN b.qty ELSE 0 END) AS bottomless_units,
       sum(b.qty) AS total_units
FROM v_zindiya_drinks_base b
WHERE b.accounting_group_name = 'Non-Alcoholic Drinks'
GROUP BY b.site, b.name
ORDER BY total_units DESC;

-- ---------- ORDER REPORT (union, dashboard table) ----------------------
CREATE OR REPLACE VIEW v_zindiya_drinks_order_report AS
SELECT * FROM (
  SELECT site, 'Draught Beer'::text AS category, keg_name AS product,
         pints_sold AS consumed, 'pints'::text AS unit, litres_sold AS litres,
         NULL::numeric AS bottomless, kegs_to_order AS to_order, 'kegs'::text AS order_unit
  FROM v_zindiya_drinks_kegs
  UNION ALL
  SELECT site, 'Packaged Beer/Cider', product, units_sold, 'units', NULL,
         NULL, units_to_order, 'units'
  FROM v_zindiya_drinks_packaged
  UNION ALL
  SELECT site, 'Spirit', spirit, total_ml, 'ml', NULL,
         NULL, bottles_to_order, '700ml bottles'
  FROM v_zindiya_drinks_spirits
  UNION ALL
  SELECT site, 'Wine', wine_name, ml_poured, 'ml', NULL,
         NULL, bottles_to_order, '75cl bottles'
  FROM v_zindiya_drinks_wine
  UNION ALL
  SELECT site, 'Cocktail', cocktail_name, total_serves, 'serves', NULL,
         bottomless_serves, NULL, ''
  FROM v_zindiya_drinks_cocktails
  UNION ALL
  SELECT site, 'Soft Drink', product, total_units, 'units', NULL,
         bottomless_units, NULL, ''
  FROM v_zindiya_drinks_softs
) t
ORDER BY site,
  CASE category
    WHEN 'Draught Beer' THEN 1 WHEN 'Packaged Beer/Cider' THEN 2
    WHEN 'Spirit' THEN 3 WHEN 'Wine' THEN 4
    WHEN 'Cocktail' THEN 5 WHEN 'Soft Drink' THEN 6 ELSE 9 END,
  consumed DESC;

-- ---------- INTEL (daily trend, all-time) ------------------------------
CREATE OR REPLACE VIEW v_zindiya_drinks_intel AS
SELECT (time_of_sale AT TIME ZONE 'Europe/London')::date AS sale_day,
       'Zindiya'::text AS site,
       CASE accounting_group_name
         WHEN 'Alcoholic Drinks' THEN 'Alcoholic'
         WHEN 'Non-Alcoholic Drinks' THEN 'Non-Alcoholic'
         ELSE accounting_group_name END AS category,
       name AS product,
       sum(quantity) AS units,
       round(sum(quantity * menu_list_price),2) AS est_revenue
FROM sales_lines
WHERE quantity > 0
  AND accounting_group_name IN ('Alcoholic Drinks','Non-Alcoholic Drinks')
GROUP BY 1,3,4;
