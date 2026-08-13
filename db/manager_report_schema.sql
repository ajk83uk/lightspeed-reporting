-- Manager's Daily Report: end-of-night report a duty manager submits per site.
-- Source: a single Google Form responses sheet (all 5 sites in one tab; the
-- site is a column, not a separate sheet like the cash-off forms).
-- One row per (site, shift date); a later submission for the same night wins.
--
-- Only the fields surfaced on the Last Night dashboard are given real columns;
-- everything the form captures is also kept verbatim in raw (jsonb) so we can
-- surface more later without a re-ingest.

CREATE TABLE IF NOT EXISTS manager_daily_report (
    site                TEXT        NOT NULL,   -- form col B ("What site...")
    business_date       DATE        NOT NULL,   -- form col C ("Date of Shift")
    manager_name        TEXT,                   -- col D
    positive_highlights TEXT,                   -- col F
    stockout_detail     TEXT,                   -- col H (critical stockouts/supplies)
    equipment_detail    TEXT,                   -- col J (equipment/maintenance issue)
    customer_comments   TEXT,                   -- col L (notable customer comments)
    incidents           TEXT,                   -- col T (incidents/accidents; free text)
    further_comments    TEXT,                   -- col U (anything else to be aware of)
    submitted_at        TIMESTAMPTZ,            -- col A (form timestamp; latest-wins)
    raw                 JSONB,                  -- every mapped form field, verbatim
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (site, business_date)
);

CREATE INDEX IF NOT EXISTS idx_manager_report_date
    ON manager_daily_report (business_date);
