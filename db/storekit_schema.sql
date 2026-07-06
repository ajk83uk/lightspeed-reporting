-- StoreKit online orders (webhook push: order.created + lifecycle events)
-- Source: StoreKit webhooks (Svix), POST -> ingest/storekit_webhook.py
-- One row per online order, current-state, upserted on every delivery.
-- order.created carries the full financial payload; later lifecycle events
-- (accepted/rejected/canceled/completed/refund.created) only patch status.
--
-- Money is stored in PENCE (minor units) exactly as StoreKit sends it. The £
-- conversion happens in db/views_storekit.sql so cards stay simple.
--
-- StoreKit is FORWARD-ONLY: webhooks carry no history, so reporting builds from
-- go-live. Any pre-launch backfill would come from the StoreKit Sales Report CSV.

-- ---------------------------------------------------------------------------
-- Site map: StoreKit venue.id -> reporting label, and (once known) the matching
-- Lightspeed business_location_id so StoreKit can join to LS sales/Nory/FT.
-- venue.id is NOT known until the first order/webhook arrives, so this is left
-- UNSEEDED. Fill it once with the five real ids (see the template below), or let
-- the views fall back to the venue.name carried on each order row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS storekit_site_map (
    venue_id             bigint PRIMARY KEY,
    site_name            text NOT NULL,       -- the site it rolls up to in reporting
    business_location_id bigint,              -- resolved from sites.nickname below
    is_core              boolean NOT NULL DEFAULT true,
    updated_at           timestamptz NOT NULL DEFAULT now()
);

-- TEMPLATE — fill in the real venue.id for each store, then run this block once.
-- (Grab each id from the first test order, or from the StoreKit dashboard URL.)
-- INSERT INTO storekit_site_map (venue_id, site_name) VALUES
--     (0000, 'Solihull'),
--     (0000, 'Peterborough'),
--     (0000, 'Portsmouth'),
--     (0000, 'Bournemouth'),
--     (0000, 'Southampton')
-- ON CONFLICT (venue_id) DO UPDATE SET
--     site_name = EXCLUDED.site_name, updated_at = now();

-- Resolve business_location_id from the master sites dimension by name
-- containment (sites.nickname is the POS label, e.g. "Tap Solihull").
-- Safe to re-run; fills whatever ids already exist in `sites`.
UPDATE storekit_site_map m
   SET business_location_id = s.business_location_id, updated_at = now()
  FROM sites s
 WHERE s.nickname ILIKE '%' || m.site_name || '%'
   AND (m.business_location_id IS DISTINCT FROM s.business_location_id);

-- ---------------------------------------------------------------------------
-- One row per online order.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS storekit_orders (
    order_id            text PRIMARY KEY,         -- StoreKit "ord_..." id
    venue_id            bigint,
    venue_name          text,                     -- carried on the payload (fallback label)
    venue_slug          text,
    code                text,                     -- short collection code (e.g. A1B2)
    order_type          text,                     -- Pickup / Delivery / Dine-in
    status              text NOT NULL DEFAULT 'created',  -- created/accepted/preparing/ready_for_pickup/out_for_delivery/completed/rejected/canceled
    is_refunded         boolean NOT NULL DEFAULT false,
    refund_total_pence  bigint,                   -- when a refund event carries an amount
    asap                boolean,
    created_at          timestamptz,              -- StoreKit order createdAt (UTC)
    delivery_time       timestamptz,
    -- Financials (PENCE). total INCLUDES tip + deliveryFee.
    total_pence         bigint,
    tip_pence           bigint,
    delivery_fee_pence  bigint,
    discount_pence      bigint,
    -- Net of tip + delivery, computed once and stored. discountTotal is already
    -- reflected in total, so it is not subtracted again here.
    net_sales_pence     bigint GENERATED ALWAYS AS (
        COALESCE(total_pence,0) - COALESCE(tip_pence,0) - COALESCE(delivery_fee_pence,0)
    ) STORED,
    -- Customer (minimal PII).
    customer_first      text,
    customer_last       text,
    customer_email      text,
    customer_phone      text,
    marketing_consent   boolean,
    covers              integer,                  -- table.covers (dine-in)
    items               jsonb,                    -- raw items[] for product-level later
    raw                 jsonb,                    -- full order.created data block
    last_event          text,                     -- last event type applied
    last_svix_id        text,
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_storekit_orders_created ON storekit_orders (created_at);
CREATE INDEX IF NOT EXISTS idx_storekit_orders_venue_created ON storekit_orders (venue_id, created_at);
CREATE INDEX IF NOT EXISTS idx_storekit_orders_status ON storekit_orders (status);

-- ---------------------------------------------------------------------------
-- Webhook dedupe log. StoreKit/Svix deliver AT-LEAST-ONCE, so we record every
-- svix-id and skip any we have already processed (idempotency.md guidance).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS storekit_webhook_events (
    svix_id      text PRIMARY KEY,
    event_type   text,
    order_id     text,
    received_at  timestamptz NOT NULL DEFAULT now()
);

-- Tie StoreKit into the master site dimension for unified views (mirrors the
-- ft_site_code column added by the bookings schema).
ALTER TABLE sites ADD COLUMN IF NOT EXISTS storekit_venue_id bigint;
