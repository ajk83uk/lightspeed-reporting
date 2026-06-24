-- Favourite Table booking reporting views.
-- Grain conventions: bookings key on site_name (2102 already folded into
-- Bournemouth by the importer) + booking_date. Cross-source joins use
-- business_location_id (the master key), since sites.nickname is the POS label
-- ("Tap Solihull") not the plain site name.

-- Drop first so column reorders/renames don't trip CREATE OR REPLACE on re-run.
DROP VIEW IF EXISTS v_unified_site_day, v_bookings_site_day, v_bookings CASCADE;

-- ---------------------------------------------------------------------------
-- One clean row per booking, with lifecycle flags + time buckets.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_bookings AS
SELECT
    b.ft_site_code,
    b.booking_ref_no,
    b.booking_code,
    b.business_location_id,
    b.site_name                                   AS site,
    b.booking_date,
    date_trunc('month', b.booking_date)::date     AS booking_month,
    trim(to_char(b.booking_date, 'Day'))          AS day_of_week,
    EXTRACT(ISODOW FROM b.booking_date)::int       AS dow_iso,   -- 1=Mon .. 7=Sun
    b.booking_time,
    EXTRACT(HOUR FROM b.booking_time)::int          AS booking_hour,
    b.duration_mins,
    b.guest_count                                  AS covers,
    b.status_code,
    b.status,
    b.sale_channel_code,
    b.sale_channel,
    b.interface_type,
    -- normalised channel for reporting: third-party resolves to its source
    CASE WHEN b.sale_channel_code = 103 THEN COALESCE(b.interface_type, 'Third-party')
         ELSE b.sale_channel END                   AS channel,
    b.table_no,
    b.is_reward_member,
    b.visits,
    (b.visits IS NOT NULL AND b.visits > 1)        AS is_returning,
    b.total_amount,
    b.deposit,
    -- lifecycle flags
    (b.status IN ('Show', 'Complete'))             AS showed,
    (b.status = 'NoShow')                          AS is_noshow,
    (b.status = 'Cancelled')                       AS is_cancelled,
    -- "live" = a booking that was expected to turn up (not cancelled)
    (b.status IS DISTINCT FROM 'Cancelled')        AS is_live
FROM bookings b;

-- ---------------------------------------------------------------------------
-- Per site per day: the booking headline numbers + no-show / cancellation
-- rates and channel mix. This is the booking fact table for dashboards.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_bookings_site_day AS
SELECT
    site,
    business_location_id,
    booking_date,
    EXTRACT(ISODOW FROM booking_date)::int                         AS dow_iso,
    COUNT(*)                                                       AS bookings,
    COUNT(*) FILTER (WHERE is_live)                                AS live_bookings,
    SUM(covers)                                                    AS booked_covers,
    SUM(covers) FILTER (WHERE is_live)                             AS live_covers,
    SUM(covers) FILTER (WHERE showed)                             AS shown_covers,
    COUNT(*)    FILTER (WHERE is_noshow)                           AS noshow_bookings,
    SUM(covers) FILTER (WHERE is_noshow)                          AS noshow_covers,
    COUNT(*)    FILTER (WHERE is_cancelled)                        AS cancelled_bookings,
    SUM(covers) FILTER (WHERE is_cancelled)                       AS cancelled_covers,
    -- channel mix (counts of live bookings)
    COUNT(*) FILTER (WHERE channel = 'Web'     AND is_live)        AS web_bookings,
    COUNT(*) FILTER (WHERE channel = 'Phone'   AND is_live)        AS phone_bookings,
    COUNT(*) FILTER (WHERE channel = 'Walk-in' AND is_live)        AS walkin_bookings,
    COUNT(*) FILTER (WHERE sale_channel_code = 103 AND is_live)    AS thirdparty_bookings,
    ROUND(AVG(duration_mins) FILTER (WHERE showed), 0)            AS avg_duration_mins,
    -- rates (over live, expected-to-arrive bookings)
    ROUND(100.0 * SUM(covers) FILTER (WHERE is_noshow)
          / NULLIF(SUM(covers) FILTER (WHERE is_live), 0), 1)      AS noshow_rate_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE is_cancelled)
          / NULLIF(COUNT(*), 0), 1)                                AS cancel_rate_pct
FROM v_bookings
GROUP BY site, business_location_id, booking_date;

-- ---------------------------------------------------------------------------
-- THE UNIFIED VIEW: per site per day, Lightspeed sales x FT bookings x
-- Sentiment reviews on one row. Joined on business_location_id + date.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_unified_site_day AS
WITH receipts AS (   -- sales header: POS covers + receipt count
    SELECT business_location_id,
           (time_closed AT TIME ZONE 'Europe/London')::date AS biz_date,
           COUNT(*)        AS receipts,
           SUM(nb_covers)  AS pos_covers
    FROM sales
    WHERE COALESCE(cancelled, false) = false
      AND time_closed IS NOT NULL
    GROUP BY 1, 2
),
revenue AS (         -- sales lines: net revenue
    SELECT business_location_id,
           (time_of_sale AT TIME ZONE 'Europe/London')::date AS biz_date,
           SUM(net_with_tax) AS revenue
    FROM sales_lines
    WHERE time_of_sale IS NOT NULL
    GROUP BY 1, 2
),
sales_day AS (
    SELECT COALESCE(r.business_location_id, v.business_location_id) AS business_location_id,
           COALESCE(r.biz_date, v.biz_date)                         AS biz_date,
           r.receipts, r.pos_covers, v.revenue
    FROM receipts r
    FULL JOIN revenue v
      ON v.business_location_id = r.business_location_id AND v.biz_date = r.biz_date
),
site_dim AS (        -- blid -> clean site label (2082 + 2102 both = Bournemouth)
    SELECT DISTINCT business_location_id, site_name
    FROM ft_site_map
    WHERE business_location_id IS NOT NULL
)
SELECT
    COALESCE(bk.business_location_id, sd.business_location_id)        AS business_location_id,
    COALESCE(dim.site_name, bk.site)                                 AS site,
    COALESCE(bk.booking_date, sd.biz_date)                           AS biz_date,
    -- sales
    sd.revenue,
    sd.pos_covers,
    sd.receipts,
    -- bookings
    bk.bookings,
    bk.live_bookings,
    bk.booked_covers,
    bk.live_covers,
    bk.shown_covers,
    bk.noshow_bookings,
    bk.noshow_covers,
    bk.cancelled_bookings,
    bk.noshow_rate_pct,
    bk.cancel_rate_pct,
    bk.web_bookings,
    bk.phone_bookings,
    bk.walkin_bookings,
    bk.thirdparty_bookings,
    bk.avg_duration_mins,
    -- cross-source derived metrics
    ROUND(sd.revenue / NULLIF(bk.booked_covers, 0), 2)  AS spend_per_booked_cover,
    ROUND(sd.revenue / NULLIF(sd.pos_covers, 0), 2)     AS spend_per_pos_cover,
    -- booked vs walked-in: how much of the room was reserved ahead
    ROUND(100.0 * bk.live_covers / NULLIF(sd.pos_covers, 0), 1) AS booked_share_of_pos_pct,
    -- reviews (daily sentiment feed; NULL until that feed lands grain='day')
    sen.rating,
    sen.nps,
    sen.reviews
FROM v_bookings_site_day bk
FULL JOIN sales_day sd
  ON sd.business_location_id = bk.business_location_id AND sd.biz_date = bk.booking_date
LEFT JOIN site_dim dim
  ON dim.business_location_id = COALESCE(bk.business_location_id, sd.business_location_id)
LEFT JOIN v_sentiment_overview sen
  ON sen.grain = 'day'
 AND sen.business_location_id = COALESCE(bk.business_location_id, sd.business_location_id)
 AND sen.period_start = COALESCE(bk.booking_date, sd.biz_date);
