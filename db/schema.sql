-- Lightspeed K-Series reporting warehouse
-- Postgres schema. Single source of truth that the ingestion worker writes to
-- and Metabase reads from.
--
-- Money is stored as NUMERIC(14,4) (Lightspeed returns up to 6dp; 4 is plenty
-- for GBP reporting). Quantities are NUMERIC(14,3) because Lightspeed sends
-- fractional/weight quantities as strings with 3 decimal places.

BEGIN;

-- ---------------------------------------------------------------------------
-- Reference: business locations (the 5 Tap & Tandoor sites)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sites (
    business_location_id BIGINT PRIMARY KEY,
    business_name        TEXT,
    nickname             TEXT,          -- 'Solihull', 'Peterborough', etc.
    active               BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Reference: accounting groups (seeded from the back office; see seed file)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS accounting_groups (
    accounting_group_id   BIGINT,
    name                  TEXT NOT NULL,
    business_location_id  BIGINT,        -- NULL = applies to all sites
    PRIMARY KEY (name)
);

-- ---------------------------------------------------------------------------
-- Item catalogue (master data from the Items endpoint, per location)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS items (
    business_location_id  BIGINT NOT NULL,
    item_id               BIGINT NOT NULL,
    sku                   TEXT,
    name                  TEXT,
    docket_name           TEXT,
    accounting_group_id   BIGINT,
    accounting_group_name TEXT,
    statistic_groups      JSONB,         -- raw statisticGroups[] for later use
    cost_price            NUMERIC(14,4),
    item_type             TEXT,          -- ITEM / SEQUENCE / GROUP / SUB_ITEM
    active                BOOLEAN,
    raw                   JSONB,         -- full payload, so nothing is lost
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (business_location_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_items_sku  ON items (sku);
CREATE INDEX IF NOT EXISTS idx_items_ag   ON items (accounting_group_name);

-- ---------------------------------------------------------------------------
-- Sales (one row per closed account/receipt)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sales (
    business_location_id  BIGINT NOT NULL,
    account_reference     TEXT   NOT NULL,
    receipt_id            TEXT,
    time_opening          TIMESTAMPTZ,
    time_closed           TIMESTAMPTZ,    -- ranking/time filters key off this
    cancelled             BOOLEAN,
    dine_in               BOOLEAN,
    nb_covers             INTEGER,
    table_name            TEXT,
    owner_name            TEXT,
    device_name           TEXT,
    raw                   JSONB,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (business_location_id, account_reference)
);
CREATE INDEX IF NOT EXISTS idx_sales_time ON sales (business_location_id, time_closed);

-- ---------------------------------------------------------------------------
-- Sales lines (one row per item line on a receipt) -- the reporting workhorse
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sales_lines (
    business_location_id    BIGINT NOT NULL,
    account_reference       TEXT   NOT NULL,
    line_id                 TEXT   NOT NULL,
    sku                     TEXT,
    name                    TEXT,
    quantity                NUMERIC(14,3),
    net_with_tax            NUMERIC(14,4),  -- totalNetAmountWithTax
    net_without_tax         NUMERIC(14,4),  -- totalNetAmountWithoutTax
    menu_list_price         NUMERIC(14,4),
    unit_cost_price         NUMERIC(14,4),
    tax_amount              NUMERIC(14,4),
    discount_amount         NUMERIC(14,4),
    accounting_group_id     BIGINT,
    accounting_group_name   TEXT,
    revenue_center          TEXT,
    time_of_sale            TIMESTAMPTZ,
    raw                     JSONB,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (business_location_id, account_reference, line_id)
);
CREATE INDEX IF NOT EXISTS idx_lines_ag   ON sales_lines (accounting_group_name);
CREATE INDEX IF NOT EXISTS idx_lines_name ON sales_lines (lower(name));

-- ---------------------------------------------------------------------------
-- Payments (one row per payment on a receipt) -- source of tips
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payments (
    business_location_id  BIGINT NOT NULL,
    account_reference     TEXT   NOT NULL,
    payment_uuid          TEXT   NOT NULL,
    code                  TEXT,
    description           TEXT,
    payment_method_id     BIGINT,
    net_with_tax          NUMERIC(14,4),
    tip                   NUMERIC(14,4),
    surcharge             NUMERIC(14,4),
    type                  TEXT,
    raw                   JSONB,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (business_location_id, account_reference, payment_uuid)
);
CREATE INDEX IF NOT EXISTS idx_pay_time ON payments (business_location_id);

-- ---------------------------------------------------------------------------
-- Cash-off forms (manager-entered Google Sheets): the bits Lightspeed lacks —
-- delivery sales by channel, wage cost, and counted cash vs expected.
-- One row per site per trading day. Loaded by ingest/cashoff.py.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cashoff_daily (
    site            TEXT NOT NULL,
    business_date   DATE NOT NULL,
    total_sales     NUMERIC(14,2),   -- manager-declared (cross-check vs Lightspeed)
    card_sales      NUMERIC(14,2),
    online_orders   NUMERIC(14,2),
    uber_eats       NUMERIC(14,2),
    just_eat        NUMERIC(14,2),
    deliveroo       NUMERIC(14,2),
    petty_cash      NUMERIC(14,2),
    expected_cash   NUMERIC(14,2),
    actual_cash     NUMERIC(14,2),
    cash_variance   NUMERIC(14,2),   -- actual - expected (+over / -short)
    card_tips       NUMERIC(14,2),
    cash_tips       NUMERIC(14,2),
    service_charge  NUMERIC(14,2),   -- Zindiya form only; counts toward tips
    covers          NUMERIC(14,2),
    wage_cost       NUMERIC(14,2),   -- from RotaReady, via the form
    cashed_up_by    TEXT,
    discrepancy_note TEXT,
    raw             JSONB,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (site, business_date)
);
-- Added later; CREATE IF NOT EXISTS won't touch existing DBs, so patch in place.
ALTER TABLE cashoff_daily ADD COLUMN IF NOT EXISTS service_charge NUMERIC(14,2);

-- ---------------------------------------------------------------------------
-- Category rules -- how raw lines roll up into report categories.
-- Two kinds of rules:
--   * 'wet_dry' dimension: classify by accounting group.
--   * named item categories (poppadoms / cocktails / desserts): match on
--     accounting group + sku list or name pattern.
-- A line can match multiple categories (it has both a wet/dry tag AND may be
-- a 'dessert'). Rules are evaluated by the v_line_category view.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS category_rules (
    id            BIGSERIAL PRIMARY KEY,
    dimension     TEXT NOT NULL,        -- 'wet_dry' or 'item_category'
    category      TEXT NOT NULL,        -- 'wet','dry','poppadoms','cocktails','desserts'...
    match_type    TEXT NOT NULL,        -- 'accounting_group','sku','name_like'
    match_value   TEXT NOT NULL,        -- group name, exact sku, or ILIKE pattern
    priority      INTEGER NOT NULL DEFAULT 100,  -- lower wins on conflict
    active        BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_rules_dim ON category_rules (dimension, active);

-- ---------------------------------------------------------------------------
-- Ingestion bookkeeping (track watermark per site so we can do incrementals)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_state (
    business_location_id  BIGINT NOT NULL,
    resource              TEXT   NOT NULL,   -- 'sales' / 'items'
    last_run_at           TIMESTAMPTZ,
    last_watermark        TIMESTAMPTZ,       -- max time_closed pulled so far
    PRIMARY KEY (business_location_id, resource)
);

-- ---------------------------------------------------------------------------
-- OAuth refresh-token store (single row).
-- Lightspeed ROTATES the refresh token on every refresh, so the latest one
-- must be persisted centrally -- env vars are read-only at runtime and each
-- cron run is a fresh process. LS_REFRESH_TOKEN in the env is only the initial
-- seed; after that the live token lives here.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS oauth_token (
    id             INTEGER PRIMARY KEY DEFAULT 1,
    refresh_token  TEXT,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT oauth_token_singleton CHECK (id = 1)
);

-- ---------------------------------------------------------------------------
-- Staff shifts (Staff API: clock-in/out). One row per shift; clock_in/clock_out
-- are derived from the shift's events[] (CLOCK_IN / CLOCK_OUT). staff_id maps to
-- sales.ownerId / sales_lines.staffId (-> server name). Powers per-hour metrics.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staff_shifts (
    business_location_id  BIGINT NOT NULL,
    shift_uuid            TEXT   NOT NULL,
    staff_id              BIGINT,
    clock_in              TIMESTAMPTZ,
    clock_out             TIMESTAMPTZ,
    date_in_utc           TIMESTAMPTZ,
    raw                   JSONB,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (business_location_id, shift_uuid)
);
CREATE INDEX IF NOT EXISTS idx_shifts_staff ON staff_shifts (staff_id);

-- ---------------------------------------------------------------------------
-- Sentiment Search (review/reputation feed). Two file types per the vendor:
--   * review-level rows  -> sentiment_reviews
--   * monthly/daily site metrics -> sentiment_overview
-- Loaded by ingest/sentiment.py. The files identify sites by full label
-- ('Tap and Tandoor X'); sentiment_site_map bridges that to the warehouse
-- (business_location_id + the short nickname cashoff_daily.site uses), so
-- reviews/ratings join to sales, items and cash-off.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sentiment_site_map (
    sentiment_label      TEXT PRIMARY KEY,    -- 'Tap and Tandoor Solihull' as in the files
    business_location_id BIGINT,              -- FK-ish to sites(); NULL until a site exists
    nickname             TEXT                 -- short name, matches cashoff_daily.site
);

-- Seed the five Tap & Tandoor sites (idempotent). business_location_ids match
-- the sites table; nickname matches cashoff_daily.site so the sources line up.
INSERT INTO sentiment_site_map (sentiment_label, business_location_id, nickname) VALUES
    ('Tap and Tandoor Bournemouth',  1718940401139714, 'Bournemouth'),
    ('Tap and Tandoor Peterborough', 1718940401139719, 'Peterborough'),
    ('Tap and Tandoor Portsmouth',   1718940401139718, 'Portsmouth'),
    ('Tap and Tandoor Solihull',     1718940401139720, 'Solihull'),
    ('Tap and Tandoor Southampton',  1718940401139717, 'Southampton')
ON CONFLICT (sentiment_label) DO NOTHING;

-- Review-level rows. No vendor review ID yet, so the PK is a content hash:
-- sha256(business|source|date|user|text). Switch to the real ID when Prithvi
-- ships it (a stable ID would also let edited reviews update in place).
CREATE TABLE IF NOT EXISTS sentiment_reviews (
    review_hash      TEXT PRIMARY KEY,
    sentiment_label  TEXT NOT NULL,
    source           TEXT,                    -- Google / Typeform / Trip Advisor / Facebook
    review_date      DATE NOT NULL,
    rating           NUMERIC(3,1),
    review_text      TEXT,
    reviewer         TEXT,
    source_file      TEXT,
    loaded_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sr_site_date ON sentiment_reviews (sentiment_label, review_date);
CREATE INDEX IF NOT EXISTS idx_sr_source    ON sentiment_reviews (source);

-- Aggregated site metrics. Historical grain is monthly (the file = the month);
-- the future daily feed will land grain='day'. raw keeps the *Comparison delta
-- columns and anything not promoted to a typed column.
CREATE TABLE IF NOT EXISTS sentiment_overview (
    sentiment_label       TEXT NOT NULL,
    period_start          DATE NOT NULL,       -- 1st of month (historical) or the day (daily)
    grain                 TEXT NOT NULL,       -- 'month' | 'day'
    reviews               INTEGER,
    rating                NUMERIC(3,2),
    competitor_rating     NUMERIC(3,2),
    star5 INTEGER, star4 INTEGER, star3 INTEGER, star2 INTEGER, star1 INTEGER,
    nps                   NUMERIC(6,2),
    critical              INTEGER,
    food_sentiment        NUMERIC(4,1), food_mentions        INTEGER,
    service_sentiment     NUMERIC(4,1), service_mentions     INTEGER,
    ambience_sentiment    NUMERIC(4,1), ambience_mentions    INTEGER,
    cleanliness_sentiment NUMERIC(4,1), cleanliness_mentions INTEGER,
    drinks_sentiment      NUMERIC(4,1), drinks_mentions      INTEGER,
    cost_sentiment        NUMERIC(4,1), cost_mentions        INTEGER,
    raw                   JSONB,
    source_file           TEXT,
    loaded_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (sentiment_label, period_start, grain)
);

-- Per-file ingest log: lets the loader skip a file it has already processed
-- (matched by name + content hash), so re-running is safe.
CREATE TABLE IF NOT EXISTS sentiment_files (
    filename   TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    kind       TEXT,                            -- 'reviews' | 'overview'
    row_count  INTEGER,
    loaded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (filename, sha256)
);

COMMIT;
