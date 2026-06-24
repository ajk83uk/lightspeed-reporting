-- Basket / market-basket analysis views (Metabase "Basket Analysis" dashboard 233).
-- Basket = one receipt (business_location_id, account_reference). Rolling 60 days.
-- Only real menu items: excludes dot-prefixed modifier lines and non Food/Drink
-- accounting groups. Re-runnable.

BEGIN;

-- Per-basket item membership (the base every other view builds on).
CREATE OR REPLACE VIEW v_basket_base AS
SELECT DISTINCT sl.business_location_id bl, sl.account_reference ar,
  regexp_replace(rtrim(st.nickname,'.'),'^Tap ','') AS site,
  sl.name AS item, sl.accounting_group_name agn
FROM sales_lines sl
JOIN sales s  ON s.business_location_id=sl.business_location_id AND s.account_reference=sl.account_reference
JOIN sites st ON st.business_location_id=sl.business_location_id
WHERE COALESCE(s.cancelled,false)=false AND sl.quantity>0 AND left(sl.name,1)<>'.'
  AND sl.accounting_group_name IN ('Food','Alcoholic Drinks','Non-Alcoholic Drinks')
  AND s.time_closed >= (CURRENT_DATE - 60);

-- Item pairs across ALL sites: support, confidence (both directions), lift.
CREATE OR REPLACE VIEW v_basket_pairs_all AS
WITH tot AS (SELECT COUNT(*) n FROM (SELECT DISTINCT bl,ar FROM v_basket_base) z),
singles AS (SELECT item, COUNT(*) sc FROM v_basket_base GROUP BY 1),
pairs AS (SELECT a.item i1,b.item i2,COUNT(*) pc FROM v_basket_base a
            JOIN v_basket_base b ON a.bl=b.bl AND a.ar=b.ar AND a.item<b.item
          GROUP BY 1,2 HAVING COUNT(*)>=10)
SELECT p.i1 AS "Item A", p.i2 AS "Item B", p.pc AS "Baskets",
  round(100.0*p.pc/(SELECT n FROM tot),1) AS "Support %",
  round(100.0*p.pc/s1.sc,0) AS "A→B %", round(100.0*p.pc/s2.sc,0) AS "B→A %",
  round((p.pc::numeric/(SELECT n FROM tot))/((s1.sc::numeric/(SELECT n FROM tot))*(s2.sc::numeric/(SELECT n FROM tot))),2) AS "Lift"
FROM pairs p JOIN singles s1 ON s1.item=p.i1 JOIN singles s2 ON s2.item=p.i2;

-- Same, but per site (lift computed within each site's baskets).
CREATE OR REPLACE VIEW v_basket_pairs_site AS
WITH tot AS (SELECT site, COUNT(*) n FROM (SELECT DISTINCT site,bl,ar FROM v_basket_base) z GROUP BY site),
singles AS (SELECT site,item,COUNT(*) sc FROM v_basket_base GROUP BY 1,2),
pairs AS (SELECT a.site,a.item i1,b.item i2,COUNT(*) pc FROM v_basket_base a
            JOIN v_basket_base b ON a.bl=b.bl AND a.ar=b.ar AND a.item<b.item
          GROUP BY 1,2,3 HAVING COUNT(*)>=6)
SELECT p.site AS site, p.i1 AS "Item A", p.i2 AS "Item B", p.pc AS "Baskets",
  round(100.0*p.pc/t.n,1) AS "Support %",
  round(100.0*p.pc/s1.sc,0) AS "A→B %", round(100.0*p.pc/s2.sc,0) AS "B→A %",
  round((p.pc::numeric/t.n)/((s1.sc::numeric/t.n)*(s2.sc::numeric/t.n)),2) AS "Lift"
FROM pairs p JOIN tot t ON t.site=p.site
JOIN singles s1 ON s1.site=p.site AND s1.item=p.i1
JOIN singles s2 ON s2.site=p.site AND s2.item=p.i2;

-- Attachment rates: of baskets containing a main course, what % also have
-- rice / naan / poppadoms / a drink. "main" = a Food line that isn't a side,
-- bread, rice, poppadom, chutney, dessert, croquette or bowl (name heuristic).
CREATE OR REPLACE VIEW v_basket_attach AS
WITH flags AS (
  SELECT bl, ar, site,
    bool_or(agn='Food' AND item !~* '(rice|naan|roti|paratha|poppadom|papad|pappad|chutney|raita|pickle|fries|chips|kulfi|gulab|jamun|brownie|ice cream|dessert|croquet|bowl)') AS has_main,
    bool_or(item ~* 'rice') has_rice,
    bool_or(item ~* 'naan') has_naan,
    bool_or(item ~* 'poppadom|papad|pappad') has_pop,
    bool_or(agn IN ('Alcoholic Drinks','Non-Alcoholic Drinks')) has_drink
  FROM v_basket_base GROUP BY 1,2,3)
SELECT site,
  COUNT(*) FILTER (WHERE has_main) AS "Main baskets",
  round(100.0*COUNT(*) FILTER (WHERE has_main AND has_rice) /NULLIF(COUNT(*) FILTER (WHERE has_main),0),0) AS "Rice attach %",
  round(100.0*COUNT(*) FILTER (WHERE has_main AND has_naan) /NULLIF(COUNT(*) FILTER (WHERE has_main),0),0) AS "Naan attach %",
  round(100.0*COUNT(*) FILTER (WHERE has_main AND has_pop)  /NULLIF(COUNT(*) FILTER (WHERE has_main),0),0) AS "Poppadom attach %",
  round(100.0*COUNT(*) FILTER (WHERE has_main AND has_drink)/NULLIF(COUNT(*) FILTER (WHERE has_main),0),0) AS "Drink attach %"
FROM flags GROUP BY site;

COMMIT;
