-- Peazi (order & pay platform) order lines -- Zindiya's stall at Saint Pauls
-- Market. Source: the console's own transactionsReport endpoint on
-- europe-west1-peazi-production.cloudfunctions.net (plain GET, no auth header;
-- params: site, tradingDateFrom/To, userId, pageSize/page, reportingLabels[],
-- paymentMethods[]). Line-level: one row per (order, PLU) with money fields
-- through to commission and payout. 'Tip' is a product line (plu 2) --
-- exclude it in food views.

CREATE TABLE IF NOT EXISTS peazi_order_lines (
    site            text NOT NULL,
    order_number    text NOT NULL,
    plu             integer NOT NULL,
    order_time      timestamptz,
    name            text,
    quantity        numeric(14,3),
    price           numeric(14,2),           -- unit price £
    total           numeric(14,2),           -- line total £ (gross)
    discount_amount numeric(14,2),
    tip             numeric(14,2),
    charge          numeric(14,2),           -- other charges
    commission      numeric(14,2),           -- Peazi/venue commission on the line
    payout          numeric(14,2),           -- what Zindiya actually receives
    payment_method  text,
    reporting_labels text,                   -- comma-joined ('Zindiya')
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site, order_number, plu)
);
CREATE INDEX IF NOT EXISTS idx_peazi_lines_time ON peazi_order_lines (order_time);
CREATE INDEX IF NOT EXISTS idx_peazi_lines_name ON peazi_order_lines (name);
