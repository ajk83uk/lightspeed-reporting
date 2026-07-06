-- Booking PACE: pre-booked (booked in advance) vs same-day (walked in / booked
-- on the day = "the uplift"), per site per day. Built from CreatedOn vs the dine
-- date, so the full 12 months of history is reconstructable. created_on may be a
-- typed column (preferred) or fall back to the raw JSON (works pre-importer-push).
CREATE OR REPLACE VIEW v_booking_pace AS
WITH b AS (
  SELECT
    site_name AS site,
    business_location_id,
    booking_date,
    guest_count AS covers,
    status,
    (status IS DISTINCT FROM 'Cancelled')                          AS is_live,
    (status IN ('Show','Complete'))                                AS showed,
    COALESCE(created_on, (raw->>'CreatedOn')::timestamptz)::date   AS created_date
  FROM bookings
)
SELECT
  site,
  business_location_id,
  booking_date AS biz_date,
  EXTRACT(ISODOW FROM booking_date)::int                                       AS dow_iso,
  trim(to_char(booking_date,'Day'))                                            AS day_of_week,
  -- pre-booked = on the books going INTO the day (created before the dine date)
  SUM(covers) FILTER (WHERE is_live AND created_date <  booking_date)          AS prebooked_covers,
  COUNT(*)    FILTER (WHERE is_live AND created_date <  booking_date)          AS prebooked_bookings,
  -- same-day = came in ON the day (the uplift)
  SUM(covers) FILTER (WHERE is_live AND created_date >= booking_date)          AS sameday_covers,
  COUNT(*)    FILTER (WHERE is_live AND created_date >= booking_date)          AS sameday_bookings,
  -- totals
  SUM(covers) FILTER (WHERE is_live)                                           AS total_booked_covers,
  SUM(covers) FILTER (WHERE showed)                                            AS shown_covers,
  SUM(covers) FILTER (WHERE status='NoShow')                                   AS noshow_covers,
  -- the headline ratio: how much of the day was reserved ahead
  ROUND(100.0 * SUM(covers) FILTER (WHERE is_live AND created_date < booking_date)
        / NULLIF(SUM(covers) FILTER (WHERE is_live),0), 1)                     AS prebook_pct
FROM b
GROUP BY site, business_location_id, booking_date;

-- Forecasting helper: by site x day-of-week, the typical pre-book/uplift shape
-- over the last 12 weeks. Use as: expected_total ~= today_prebooked / (prebook_pct/100),
-- or expected_total ~= today_prebooked + avg_sameday_covers.
CREATE OR REPLACE VIEW v_booking_pace_dow AS
SELECT
  site,
  dow_iso,
  max(day_of_week)                       AS day_of_week,
  count(*)                               AS days_sampled,
  round(avg(prebooked_covers))           AS avg_prebooked_covers,
  round(avg(sameday_covers))             AS avg_sameday_covers,   -- typical uplift
  round(avg(total_booked_covers))        AS avg_total_covers,
  round(avg(prebook_pct),1)              AS avg_prebook_pct
FROM v_booking_pace
WHERE biz_date >= current_date - 84 AND biz_date < current_date  -- last 12 wks, exclude today
GROUP BY site, dow_iso
ORDER BY site, dow_iso;
