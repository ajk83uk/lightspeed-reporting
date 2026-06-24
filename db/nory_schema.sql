-- Nory WFM labour data (S3 export: nory-data-exporter-tap-and-tandoor)
-- Source: Tap & Tandoor/{date}/{site}/wfm/labour_insights.json
-- Each per-site file is a rolling ~32-day array of daily aggregates, so the
-- nightly upsert below self-heals/backfills the last month every run.

-- Site map: Nory branch_id -> site label, and (once confirmed) the matching
-- Lightspeed business_location_id so dashboards can join Nory labour to LS sales.
CREATE TABLE IF NOT EXISTS nory_site_map (
    branch_id            text PRIMARY KEY,
    site_name            text NOT NULL,
    business_location_id bigint,          -- fill in once Nory<->LS mapping confirmed
    is_core              boolean NOT NULL DEFAULT true,   -- the 5 T&T sites
    updated_at           timestamptz NOT NULL DEFAULT now()
);

-- The 5 Tap & Tandoor sites (Vita + Head Office deliberately excluded).
INSERT INTO nory_site_map (branch_id, site_name, is_core) VALUES
    ('67e6db108f8afc001e8cdd23', 'Bournemouth',  true),
    ('67e6daee701cde0025b8d074', 'Peterborough', true),
    ('67e6da84097cea00257e151c', 'Portsmouth',   true),
    ('67e6daca000f330025929e40', 'Solihull',     true),
    ('67e6da9fb9e17500250a6ca7', 'Southampton',  true)
ON CONFLICT (branch_id) DO UPDATE SET
    site_name = EXCLUDED.site_name, is_core = EXCLUDED.is_core, updated_at = now();

-- One row per site per day: the headline labour numbers.
CREATE TABLE IF NOT EXISTS nory_labour_daily (
    branch_id           text NOT NULL,
    biz_date            date NOT NULL,
    site_name           text,
    col                 numeric,   -- actual cost of labour (£)
    planned_col         numeric,   -- scheduled/planned cost of labour (£)
    hours               numeric,   -- actual hours
    planned_hours       numeric,   -- scheduled hours
    sales               numeric,   -- Nory's own sales figure (sales of record = Lightspeed)
    orders              integer,
    percentage          numeric,   -- actual labour % of sales
    planned_percentage  numeric,   -- planned labour % of sales
    splh                numeric,   -- sales per labour hour
    oplh                numeric,   -- orders per labour hour
    raw                 jsonb,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (branch_id, biz_date)
);
CREATE INDEX IF NOT EXISTS idx_nory_labour_daily_date ON nory_labour_daily (biz_date);

-- Long format: the wage-cost breakdown (Hourly, Daily, Holiday Accrual,
-- Pension, NI Under 21, NI Over 21). Category names stored as-is so new
-- categories don't need a schema change.
CREATE TABLE IF NOT EXISTS nory_labour_breakdown (
    branch_id      text NOT NULL,
    biz_date       date NOT NULL,
    category       text NOT NULL,
    value          numeric,   -- actual £
    planned_value  numeric,   -- planned £
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (branch_id, biz_date, category)
);
