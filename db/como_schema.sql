-- Como (comosense.com) loyalty -- Export Event API webhooks (push).
-- Source: Como Hub "Export Event" rule -> POST ingest/como_webhook.py
--
-- FORWARD-ONLY: webhooks carry no history, so reporting builds from go-live.
-- Money is stored in PENCE exactly as Como sends it (£ conversion in the views).
--
-- PII POLICY: Como payloads carry name/email/phone. We do NOT store them.
-- ingest/como.py scrubs those keys from every JSON blob before it is written,
-- and keeps only a salted SHA-256 of the lower-cased email / normalised phone
-- (email_hash / phone_hash) so members can still be matched to Favourite Table
-- bookings. Consent flags (allow_sms / allow_email) are kept and honoured.

-- ---------------------------------------------------------------------------
-- Site map: Como BranchID -> reporting site.
-- Branch ids below are the five IDs seen in the Como dashboard (June 2026).
-- Three equal the Lightspeed business_location_id; Solihull + Southampton do
-- NOT, which is why this is an explicit table. The BranchID that actually
-- arrives in `purchase` payloads has to be confirmed from the first live event
-- (Como's docs sample shows a short id like "67"); add rows here if it differs.
-- Unmapped branch ids surface in the views as 'Unmapped (<id>)' so nothing
-- is silently dropped.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS como_site_map (
    como_branch_id       text PRIMARY KEY,
    site_name            text NOT NULL,
    business_location_id bigint,
    updated_at           timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Every delivery, deduped. Como has no event id, so event_id is
-- UserAction.Key when present, else sha256 of the raw request body (a retry of
-- the same event is byte-identical). payload is PII-scrubbed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS como_events (
    event_id        text PRIMARY KEY,
    trigger         text NOT NULL,            -- joinedclub / purchase / redeem / ...
    rule_name       text,
    event_at        timestamptz,              -- from the outer epoch TimeStamp (ms)
    membership_key  text,
    branch_id       text,
    payload         jsonb,                    -- scrubbed
    received_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_como_events_trigger_at ON como_events (trigger, event_at);
CREATE INDEX IF NOT EXISTS idx_como_events_member ON como_events (membership_key);

-- ---------------------------------------------------------------------------
-- One row per member, current state (upserted from any event carrying a
-- Membership block; older events never overwrite newer ones).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS como_members (
    membership_key   text PRIMARY KEY,        -- Data.MembershipKey
    como_member_id   text,
    registered_at    timestamptz,             -- Membership.CreatedOn
    registration_source text,
    status           text,                    -- MembershipStatus (registered / ...)
    consent          text,
    allow_sms        boolean,
    allow_email      boolean,
    points           bigint,                  -- as sent (Membership.Points)
    budget_weighted  bigint,                  -- as sent (credit, pence)
    tags             jsonb,
    email_hash       text,
    phone_hash       text,
    joined_event_at  timestamptz,             -- when we saw 'joinedclub'
    first_branch_id  text,                    -- BranchID of the first purchase seen
    last_event       text,
    last_event_at    timestamptz,
    first_seen_at    timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_como_members_reg ON como_members (registered_at);
CREATE INDEX IF NOT EXISTS idx_como_members_email ON como_members (email_hash);
CREATE INDEX IF NOT EXISTS idx_como_members_phone ON como_members (phone_hash);

-- ---------------------------------------------------------------------------
-- One row per loyalty-identified purchase (PURCHASE events).
-- transaction_id is the till's reference -- candidate join to Lightspeed
-- (to be proven on the first live events).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS como_transactions (
    purchase_uid     text PRIMARY KEY,        -- UserAction.PurchaseUID (else event_id)
    event_id         text,
    membership_key   text,
    branch_id        text,
    transaction_id   text,
    pos_id           text,
    source           text,                    -- POS / online / kiosk ...
    total_pence      bigint,                  -- TotalSum
    relative_sum_pence bigint,                -- RelativeSum
    num_members      integer,
    currency         text,
    purchased_at     timestamptz,
    first_seen_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_como_tx_at ON como_transactions (purchased_at);
CREATE INDEX IF NOT EXISTS idx_como_tx_branch_at ON como_transactions (branch_id, purchased_at);
CREATE INDEX IF NOT EXISTS idx_como_tx_txid ON como_transactions (branch_id, transaction_id);

-- ---------------------------------------------------------------------------
-- Redemptions (REDEEM events): one row per benefit applied.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS como_redemptions (
    redemption_id    text PRIMARY KEY,        -- event_id + ':' + index
    event_id         text,
    membership_key   text,
    branch_id        text,
    transaction_id   text,
    asset_key        text,
    asset_name       text,
    asset_type       text,                    -- gift / punch_card / ...
    applied_pence    bigint,                  -- AppliedAmount (value given away)
    result           text,
    redeemed_at      timestamptz,
    first_seen_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_como_red_at ON como_redemptions (redeemed_at);

-- Tie Como into the master site dimension (mirrors storekit_venue_id).
ALTER TABLE sites ADD COLUMN IF NOT EXISTS como_branch_id text;
