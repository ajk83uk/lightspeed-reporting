-- Nory labour views for the Last Night report.
-- Target labour cost = 30% of sales (RAG thresholds below).

-- Most recent labour day available per core site, with scheduled-vs-actual
-- variances and a RAG flag against the 30% target.
CREATE OR REPLACE VIEW v_nory_last_night AS
WITH latest AS (
    SELECT d.*,
           ROW_NUMBER() OVER (PARTITION BY d.branch_id ORDER BY d.biz_date DESC) AS rn
    FROM nory_labour_daily d
    JOIN nory_site_map m ON m.branch_id = d.branch_id AND m.is_core
)
SELECT
    site_name,
    biz_date,
    sales,
    orders,
    col                              AS labour_cost,
    planned_col                      AS planned_labour_cost,
    ROUND(col - planned_col, 2)      AS labour_cost_var,
    hours,
    planned_hours,
    ROUND(hours - planned_hours, 2)  AS hours_var,
    -- Guard: when Nory has not synced the day's sales yet, % is meaningless.
    CASE WHEN sales > 0 THEN percentage END          AS labour_pct,
    planned_percentage               AS planned_labour_pct,
    splh,
    oplh,
    -- RAG against the 30% target: red >30, amber 27-30, green <27.
    -- N/A when sales not yet synced for the day.
    CASE
        WHEN sales IS NULL OR sales = 0 THEN 'N/A'
        WHEN percentage > 30 THEN 'RED'
        WHEN percentage >= 27 THEN 'AMBER'
        ELSE 'GREEN'
    END                              AS labour_pct_rag,
    -- Secondary flag: ran materially over the day's own plan (>5% of planned cost)
    CASE
        WHEN planned_col > 0 AND (col - planned_col) / planned_col > 0.05
        THEN true ELSE false
    END                              AS over_plan_flag
FROM latest
WHERE rn = 1
ORDER BY labour_pct DESC NULLS LAST;

-- Wage-cost breakdown for the same latest night, per site & category.
CREATE OR REPLACE VIEW v_nory_last_night_breakdown AS
SELECT b.site_name, b.biz_date, b.category, b.value AS actual, b.planned_value AS planned
FROM (
    SELECT bd.*, m.site_name,
           DENSE_RANK() OVER (PARTITION BY bd.branch_id ORDER BY bd.biz_date DESC) AS dr
    FROM nory_labour_breakdown bd
    JOIN nory_site_map m ON m.branch_id = bd.branch_id AND m.is_core
) b
WHERE b.dr = 1
ORDER BY b.site_name, b.value DESC;
