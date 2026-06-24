-- Sentiment Search reporting views.
-- Bridge the vendor feed to the rest of the warehouse via sentiment_site_map,
-- which carries both the short nickname (= cashoff_daily.site) and the
-- business_location_id (= sites / sales / items), so reviews and ratings line
-- up with sales and cash-off for cross-source reporting.

-- One row per site per period (monthly for the historical backfill; the daily
-- feed will land grain='day'). Adds positive/negative review % helpers.
CREATE OR REPLACE VIEW v_sentiment_overview AS
SELECT
    o.period_start,
    o.grain,
    m.nickname              AS site,
    m.business_location_id,
    o.sentiment_label,
    o.reviews,
    o.rating,
    o.competitor_rating,
    o.star5, o.star4, o.star3, o.star2, o.star1,
    o.nps,
    o.critical,
    o.food_sentiment, o.service_sentiment, o.ambience_sentiment,
    o.cleanliness_sentiment, o.drinks_sentiment, o.cost_sentiment,
    o.food_mentions, o.service_mentions, o.ambience_mentions,
    o.cleanliness_mentions, o.drinks_mentions, o.cost_mentions,
    CASE WHEN o.reviews > 0
         THEN round(100.0 * (COALESCE(o.star5,0) + COALESCE(o.star4,0)) / o.reviews, 1)
    END AS positive_pct,
    CASE WHEN o.reviews > 0
         THEN round(100.0 * (COALESCE(o.star1,0) + COALESCE(o.star2,0)) / o.reviews, 1)
    END AS negative_pct
FROM sentiment_overview o
LEFT JOIN sentiment_site_map m USING (sentiment_label);

-- One row per review, with site keys + a month bucket and a negative flag for
-- an operational watch-list (rating <= 3).
CREATE OR REPLACE VIEW v_sentiment_reviews AS
SELECT
    r.review_date,
    date_trunc('month', r.review_date)::date AS review_month,
    m.nickname              AS site,
    m.business_location_id,
    r.sentiment_label,
    r.source,
    r.rating,
    r.reviewer,
    r.review_text,
    (r.rating <= 3) AS is_negative,
    r.review_hash
FROM sentiment_reviews r
LEFT JOIN sentiment_site_map m USING (sentiment_label);

-- Cross-source headline: monthly rating / NPS vs cash-off sales & covers per
-- site. cashoff_daily.site already matches the nickname, so it joins directly.
CREATE OR REPLACE VIEW v_sentiment_site_month AS
WITH cash AS (
    SELECT site,
           date_trunc('month', business_date)::date AS period_start,
           SUM(total_sales) AS cashoff_sales,
           SUM(covers)      AS cashoff_covers
    FROM cashoff_daily
    GROUP BY 1, 2
)
SELECT
    o.period_start,
    o.site,
    o.business_location_id,
    o.reviews,
    o.rating,
    o.nps,
    o.positive_pct,
    o.negative_pct,
    c.cashoff_sales,
    c.cashoff_covers
FROM v_sentiment_overview o
LEFT JOIN cash c ON c.site = o.site AND c.period_start = o.period_start
WHERE o.grain = 'month';
