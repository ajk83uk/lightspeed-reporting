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
    covers          NUMERIC(14,2),
    wage_cost       NUMERIC(14,2),   -- from RotaReady, via the form
    cashed_up_by    TEXT,
    discrepancy_note TEXT,
    raw             JSONB,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (site, business_date)
);

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

COMMIT;
