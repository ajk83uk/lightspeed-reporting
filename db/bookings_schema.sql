-- Favourite Table bookings (pull API: GetBookingList)
-- Source: GET {FT_BASE}/BookingApi/Booking/GetBookingList/{token}
--             ?SiteCode={code}&ShiftCode=0&BookingDate=YYYYMMDD
-- One row per booking, current-state, upserted on every pull. Back-dated pulls
-- return the FINAL status (confirmed Jaipal 2026-06-24), and BookingRefNo is
-- unique + stable across status changes, so it is a safe upsert key. A rolling
-- window re-pulled nightly self-heals late status changes (mirrors nory).

-- Site map: FT SiteCode -> site label, and (once confirmed) the matching
-- Lightspeed business_location_id so dashboards can join bookings to LS sales.
-- Bournemouth has TWO FT SiteCodes (2082 main venue + 2102 Darts & Shuffleboard);
-- both roll up to the one Bournemouth site. T&T only -- Zindiya out of scope.
CREATE TABLE IF NOT EXISTS ft_site_map (
    ft_site_code         integer PRIMARY KEY,
    site_name            text NOT NULL,      -- the site it rolls up to in reporting
    business_location_id bigint,             -- filled from sites.nickname below (NULL until known)
    is_core              boolean NOT NULL DEFAULT true,
    updated_at           timestamptz NOT NULL DEFAULT now()
);

INSERT INTO ft_site_map (ft_site_code, site_name, is_core) VALUES
    (2084, 'Solihull',     true),
    (2082, 'Bournemouth',  true),
    (2102, 'Bournemouth',  true),   -- Darts & Shuffleboard -> folds into Bournemouth
    (2083, 'Peterborough', true),
    (2086, 'Portsmouth',   true),
    (2085, 'Southampton',  true)
ON CONFLICT (ft_site_code) DO UPDATE SET
    site_name = EXCLUDED.site_name, is_core = EXCLUDED.is_core, updated_at = now();

-- Resolve business_location_id from the master sites dimension. sites.nickname
-- is the POS label ("Tap Solihull", "Tap Bournemouth."), so match by containment
-- of the plain site name. Safe to re-run: fills whatever ids exist in `sites`.
UPDATE ft_site_map m
   SET business_location_id = s.business_location_id, updated_at = now()
  FROM sites s
 WHERE s.nickname ILIKE '%' || m.site_name || '%'
   AND (m.business_location_id IS DISTINCT FROM s.business_location_id);

-- One row per booking.
CREATE TABLE IF NOT EXISTS bookings (
    ft_site_code         integer NOT NULL,
    booking_ref_no       text NOT NULL,
    booking_code         text,
    business_location_id bigint,            -- resolved via ft_site_map at ingest
    site_name            text,              -- reporting label (folds 2102 -> Bournemouth)
    booking_date         date,
    booking_time_secs    integer,           -- seconds since midnight (raw from FT)
    booking_time         time,              -- derived from booking_time_secs
    duration_mins        integer,           -- from Duration / Start-End
    guest_count          integer,           -- covers
    status_code          integer,
    status               text,              -- decoded label
    sale_channel_code    integer,
    sale_channel         text,              -- decoded label
    interface_type_code  integer,
    interface_type       text,              -- decoded label (third-party source)
    table_no             text,
    is_reward_member     boolean,           -- loyalty flag (links to Como)
    visits               integer,           -- repeat-guest count
    total_amount         numeric,
    deposit              numeric,
    -- PII kept minimal.
    first_name           text,
    last_name            text,
    email                text,
    tel                  text,
    opt_in_email         boolean,
    opt_in_mobile        boolean,
    raw                  jsonb,
    first_seen_at        timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ft_site_code, booking_ref_no)
);
CREATE INDEX IF NOT EXISTS idx_bookings_date ON bookings (booking_date);
CREATE INDEX IF NOT EXISTS idx_bookings_blid_date ON bookings (business_location_id, booking_date);
CREATE INDEX IF NOT EXISTS idx_bookings_site_date ON bookings (site_name, booking_date);

-- Tie bookings into the master site dimension for the unified view.
ALTER TABLE sites ADD COLUMN IF NOT EXISTS ft_site_code    integer;  -- primary FT code (Bournemouth = 2082)
ALTER TABLE sites ADD COLUMN IF NOT EXISTS sentiment_label text;
