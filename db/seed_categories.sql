-- Seed reference data and category rules.
-- Re-runnable: clears category_rules and accounting_groups then re-inserts.
--
-- Accounting groups taken from the Tap & Tandoor back office:
--   Alcoholic Drinks (alcohol, VAT 20%)
--   Food (normal, VAT 20%)
--   Misc (zero rated, VAT 0%)
--   Non-Alcoholic Drinks (normal, VAT 20%)
--   OA-AG (online ordering, normal with takeaway)
--   Tap Accounting Group (normal with takeaway)

BEGIN;

-- --- Accounting groups reference -------------------------------------------
DELETE FROM accounting_groups;
INSERT INTO accounting_groups (accounting_group_id, name, business_location_id) VALUES
    (NULL, 'Alcoholic Drinks',     NULL),
    (NULL, 'Food',                 NULL),
    (NULL, 'Misc',                 NULL),
    (NULL, 'Non-Alcoholic Drinks', NULL),
    (NULL, 'OA-AG',                NULL),
    (NULL, 'Tap Accounting Group', NULL);

-- --- Category rules ---------------------------------------------------------
DELETE FROM category_rules;

-- WET / DRY dimension (by accounting group). "Wet" = drinks, "Dry" = food.
INSERT INTO category_rules (dimension, category, match_type, match_value, priority) VALUES
    ('wet_dry', 'wet', 'accounting_group', 'Alcoholic Drinks',     10),
    ('wet_dry', 'wet', 'accounting_group', 'Non-Alcoholic Drinks', 10),
    ('wet_dry', 'dry', 'accounting_group', 'Food',                 10);
-- Misc / OA-AG / Tap Accounting Group are intentionally left untagged (they
-- fall into 'other' in the view). Add rules here if you want them counted.

-- ITEM CATEGORIES.
-- These live INSIDE the accounting groups above, so they are matched on name
-- pattern (and can be tightened to explicit SKUs once you've reviewed the
-- catalogue -- see helper query in db/discover_items.sql).
--
-- name_like values are passed to SQL ILIKE, so % is a wildcard.

-- Poppadoms (food) -- catch common spellings.
INSERT INTO category_rules (dimension, category, match_type, match_value, priority) VALUES
    ('item_category', 'poppadoms', 'name_like', '%poppadom%', 50),
    ('item_category', 'poppadoms', 'name_like', '%popadom%',  50),
    ('item_category', 'poppadoms', 'name_like', '%papad%',    50),
    ('item_category', 'poppadoms', 'name_like', '%pappad%',   50);

-- Desserts (food).
INSERT INTO category_rules (dimension, category, match_type, match_value, priority) VALUES
    ('item_category', 'desserts', 'name_like', '%dessert%',     50),
    ('item_category', 'desserts', 'name_like', '%kulfi%',       50),
    ('item_category', 'desserts', 'name_like', '%gulab%',       50),
    ('item_category', 'desserts', 'name_like', '%jamun%',       50),
    ('item_category', 'desserts', 'name_like', '%ice cream%',   50),
    ('item_category', 'desserts', 'name_like', '%kheer%',       50),
    ('item_category', 'desserts', 'name_like', '%brownie%',     50);

-- Cocktails (alcoholic drinks).
INSERT INTO category_rules (dimension, category, match_type, match_value, priority) VALUES
    ('item_category', 'cocktails', 'name_like', '%cocktail%',   50),
    ('item_category', 'cocktails', 'name_like', '%mojito%',     50),
    ('item_category', 'cocktails', 'name_like', '%margarita%',  50),
    ('item_category', 'cocktails', 'name_like', '%negroni%',    50),
    ('item_category', 'cocktails', 'name_like', '%martini%',    50),
    ('item_category', 'cocktails', 'name_like', '%daiquiri%',   50),
    ('item_category', 'cocktails', 'name_like', '%old fashioned%', 50),
    ('item_category', 'cocktails', 'name_like', '%spritz%',     50);

-- 2-4-1 (two-for-one) cocktails — sold as their own menu items.
-- Priority 40 (beats generic 'cocktails' 50) so a "2-4-1 Mojito" is tagged
-- as 241 first. Patterns are a starting point — verify against real names.
INSERT INTO category_rules (dimension, category, match_type, match_value, priority) VALUES
    ('item_category', '241 cocktails', 'name_like', '%2-4-1%',      40),
    ('item_category', '241 cocktails', 'name_like', '%2 for 1%',    40),
    ('item_category', '241 cocktails', 'name_like', '%2-for-1%',    40),
    ('item_category', '241 cocktails', 'name_like', '%2for1%',      40),
    ('item_category', '241 cocktails', 'name_like', '%two for one%', 40);
-- (Dropped a bare '%241%' rule: it false-matched volumes like "Coke 241ml".
--  If your 2-4-1 items use a plain "241", add them by exact SKU instead.)

COMMIT;

-- NOTE: the name patterns above are a STARTING POINT. Run db/discover_items.sql
-- against your live catalogue, eyeball the matches, then either add SKUs
-- (match_type='sku') for precision or extend the name patterns.
