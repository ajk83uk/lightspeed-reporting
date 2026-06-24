-- ============================================================================
-- Patch: surface two things we already store but don't expose as columns.
--   (1) v_report_lines  -> line-level server (the staff who rang each line)
--   (2) v_fact_payments -> payment method (code + friendly description)
--
-- Both are pure-append CREATE OR REPLACE (new columns added at the END), so
-- they will NOT break dependent views (v_staff_*, scorecard, leakage) and need
-- no DROP. Run once against Neon, then re-sync Metabase so the new columns show.
--
--   Deploy:  psql "$DATABASE_URL" -f db/patch_line_staff_and_payment_method.sql
--   Then:    Metabase > Admin > Databases > (Neon) > "Sync database schema now"
--
-- After deploying, fold these two blocks back into db/views.sql (replacing the
-- existing v_report_lines / v_fact_payments definitions) so a future migrate
-- re-applies them. This file is safe to re-run (idempotent).
-- ============================================================================

BEGIN;

-- (1) Line-level server on every report line ---------------------------------
-- line_staff differs from the receipt owner on ~40% of lines, so this is a more
-- accurate basis for upsell-attach attribution than the check owner.
CREATE OR REPLACE VIEW v_report_lines AS
SELECT fl.*,
       (SELECT ic.category
          FROM v_line_item_category ic
         WHERE ic.business_location_id = fl.business_location_id
           AND ic.account_reference    = fl.account_reference
           AND ic.line_id              = fl.line_id
         ORDER BY ic.category
         LIMIT 1) AS item_category,
       sl.raw->>'staffName'                   AS line_staff,
       NULLIF(sl.raw->>'staffId','')::bigint  AS line_staff_id
FROM v_fact_lines fl
LEFT JOIN sales_lines sl
       ON sl.business_location_id = fl.business_location_id
      AND sl.account_reference    = fl.account_reference
      AND sl.line_id              = fl.line_id;

-- (2) Payment method on fact payments ----------------------------------------
-- payments.code / .description carry the tender type; v_fact_payments already
-- joins site + business_date, so we just expose the method for the mix cards.
CREATE OR REPLACE VIEW v_fact_payments AS
SELECT
    p.business_location_id,
    site.nickname                      AS site,
    p.account_reference,
    p.payment_uuid,
    p.net_with_tax,
    p.tip,
    p.surcharge,
    sa.time_closed,
    (sa.time_closed AT TIME ZONE 'Europe/London')::date AS business_date,
    CASE
        WHEN EXTRACT(HOUR FROM (sa.time_closed AT TIME ZONE 'Europe/London')) BETWEEN 12 AND 16 THEN 'Lunch (12-5)'
        WHEN EXTRACT(HOUR FROM (sa.time_closed AT TIME ZONE 'Europe/London')) BETWEEN 17 AND 21 THEN 'Dinner (5-10)'
        ELSE 'Other'
    END AS shift,
    p.code,
    p.description                      AS payment_method,
    -- convenience bucket for cash% / card-tip reporting
    CASE
        WHEN p.code IN ('LSPAY_ADYEN_TERMINAL_API_LOCAL','LSPAY_ADYEN_TAP_TO_PAY') THEN 'Card'
        WHEN p.code = 'CASH'   THEN 'Cash'
        WHEN p.code = 'IKGIFT' THEN 'Gift card'
        WHEN p.code = 'OA'     THEN 'Online'
        WHEN p.code = 'IKDEBT' THEN 'Invoice'
        ELSE 'Other'
    END                                AS tender_group
FROM payments p
JOIN sales sa
      ON sa.business_location_id = p.business_location_id
     AND sa.account_reference    = p.account_reference
LEFT JOIN sites site
      ON site.business_location_id = p.business_location_id
WHERE COALESCE(sa.cancelled, FALSE) = FALSE;

COMMIT;
