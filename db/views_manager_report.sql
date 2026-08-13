-- Manager's Daily Report -- last night, one row per site.
-- "Last night" = yesterday's shift date in Europe/London, matching the other
-- Last Night cards. Column labels are the manager-facing question wording,
-- trimmed. Ordered by site for a stable table on the dashboard.

CREATE OR REPLACE VIEW v_manager_report_last_night AS
SELECT
    site                                        AS "Site",
    manager_name                                AS "Manager",
    positive_highlights                         AS "Positive Highlights",
    stockout_detail                             AS "Stockouts / Supply Issues",
    equipment_detail                            AS "Equipment / Maintenance",
    customer_comments                           AS "Customer Comments",
    incidents                                   AS "Incidents / Accidents",
    further_comments                            AS "Further Comments"
FROM manager_daily_report
WHERE business_date = (timezone('Europe/London', now()))::date - 1
ORDER BY site;
