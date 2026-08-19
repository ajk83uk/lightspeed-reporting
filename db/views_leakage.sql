-- Unified LEAKAGE view (voids + discounts), split out of views.sql so it can
-- be applied on its own: it needs DROP+CREATE (new column), while views.sql is
-- all CREATE OR REPLACE. Run after views.sql, before views_eotw.sql (which
-- consumes attributed_staff).

BEGIN;

-- ---------------------------------------------------------------------------
-- Unified LEAKAGE view: voided lines AND discounted lines in one table, tagged
-- by leakage_type ('Void' / 'Discount'). Single relation -> Metabase field
-- filters (date / site / staff / shift / type) map cleanly. `amount` is the
-- positive money lost (void value or discount given).
--
-- TWO staff columns, and the difference matters:
--   staff_name       = the name Lightspeed stamps on the line. On a VOID or a
--                      DISCOUNT that is whoever PERFORMED/AUTHORISED it on the
--                      till -- i.e. the manager holding the permission, not the
--                      server. Evidence (Aug 2026, 2,083 voids): 55% differ from
--                      the receipt owner, 40% name someone who appears on no
--                      other line of that receipt, and voids collapse onto one
--                      or two names per site (Peterborough 93%, Bournemouth 80%).
--                      There is no separate voidedBy/authorisedBy field in the
--                      LS payload, so this is all we get. Keep it -- it is the
--                      right measure of authorisation ACTIVITY.
--   attributed_staff = the receipt's primary server (dominant ringer by value),
--                      i.e. whose section the leakage happened on. Falls back to
--                      owner_name, then staff_name for receipts with no other
--                      lines. Same dominant-ringer rule v_staff_eotw_day uses
--                      for covers/tips, so the two agree.
-- Score people on attributed_staff; investigate patterns with staff_name.
--
-- Rebuilt (not CREATE OR REPLACE) because attributed_staff is a new column.
-- CASCADE drops v_staff_eotw_day, which views_eotw.sql recreates later in the
-- same migrate run -- checked via pg_depend, it is the only dependant.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_leakage_lines CASCADE;
CREATE VIEW v_leakage_lines AS
WITH ring AS (  -- value rung per staff per receipt, non-void lines only
    SELECT sl.business_location_id AS bl, sl.account_reference AS ar,
           NULLIF(sl.raw->>'staffName','') AS staff,
           SUM(sl.net_without_tax) AS v
    FROM sales_lines sl
    WHERE COALESCE(sl.raw->>'voidReason','') = ''
      AND NULLIF(sl.raw->>'staffName','') IS NOT NULL
    GROUP BY 1, 2, 3
),
prim AS (  -- the dominant ringer = primary server for that receipt
    SELECT bl, ar, staff FROM (
        SELECT bl, ar, staff,
               ROW_NUMBER() OVER (PARTITION BY bl, ar ORDER BY v DESC, staff) AS rn
        FROM ring
    ) t WHERE rn = 1
)
SELECT
    sl.business_location_id,
    site.nickname AS site,
    sl.account_reference, sl.line_id, sl.name,
    'Void'::text  AS leakage_type,
    sl.raw->>'voidReason' AS reason,
    COALESCE(NULLIF(sl.raw->>'staffName',''), '(unknown)') AS staff_name,
    COALESCE(p.staff, NULLIF(sa.owner_name,''),
             NULLIF(sl.raw->>'staffName',''), '(unknown)') AS attributed_staff,
    -sl.net_without_tax AS amount,
    (sa.time_closed AT TIME ZONE 'Europe/London')::date AS business_date,
    CASE
        WHEN EXTRACT(HOUR FROM (COALESCE(sl.time_of_sale, sa.time_closed) AT TIME ZONE 'Europe/London')) BETWEEN 12 AND 16 THEN 'Lunch (12-5)'
        WHEN EXTRACT(HOUR FROM (COALESCE(sl.time_of_sale, sa.time_closed) AT TIME ZONE 'Europe/London')) BETWEEN 17 AND 21 THEN 'Dinner (5-10)'
        ELSE 'Other' END AS shift,
    COALESCE(sl.time_of_sale, sa.time_closed) AS tx_time
FROM sales_lines sl
JOIN sales sa ON sa.business_location_id = sl.business_location_id AND sa.account_reference = sl.account_reference
LEFT JOIN sites site ON site.business_location_id = sl.business_location_id
LEFT JOIN prim p ON p.bl = sl.business_location_id AND p.ar = sl.account_reference
WHERE COALESCE(sa.cancelled, FALSE) = FALSE AND COALESCE(sl.raw->>'voidReason','') <> ''
UNION ALL
SELECT
    sl.business_location_id,
    site.nickname,
    sl.account_reference, sl.line_id, sl.name,
    'Discount'::text,
    COALESCE(NULLIF(sl.raw->>'discountName',''), '(unnamed)'),
    COALESCE(NULLIF(sl.raw->>'staffName',''), '(unknown)'),
    COALESCE(p.staff, NULLIF(sa.owner_name,''),
             NULLIF(sl.raw->>'staffName',''), '(unknown)'),
    sl.discount_amount,
    (sa.time_closed AT TIME ZONE 'Europe/London')::date,
    CASE
        WHEN EXTRACT(HOUR FROM (COALESCE(sl.time_of_sale, sa.time_closed) AT TIME ZONE 'Europe/London')) BETWEEN 12 AND 16 THEN 'Lunch (12-5)'
        WHEN EXTRACT(HOUR FROM (COALESCE(sl.time_of_sale, sa.time_closed) AT TIME ZONE 'Europe/London')) BETWEEN 17 AND 21 THEN 'Dinner (5-10)'
        ELSE 'Other' END,
    COALESCE(sl.time_of_sale, sa.time_closed)
FROM sales_lines sl
JOIN sales sa ON sa.business_location_id = sl.business_location_id AND sa.account_reference = sl.account_reference
LEFT JOIN sites site ON site.business_location_id = sl.business_location_id
LEFT JOIN prim p ON p.bl = sl.business_location_id AND p.ar = sl.account_reference
WHERE COALESCE(sa.cancelled, FALSE) = FALSE AND COALESCE(sl.discount_amount, 0) <> 0;

COMMIT;
