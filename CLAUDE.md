# Tap & Tandoor — Lightspeed Reporting (agent guide)

This repo ingests Lightspeed K-Series (financial-api + items), Nory (labour),
Favourite Table (bookings) and review sentiment into a **Neon Postgres** database
(`neondb`, schema `public`), which powers Metabase dashboards.

## How to read the data
Query through the **`lightspeed-db`** MCP server (configured in `.mcp.json`). It is
**read-only** — `SELECT` only, never write/DDL. The data is already ingested; you do
not need to call the Lightspeed API to answer reporting questions.

The Python pipeline (`ingest/`) is what calls the source APIs. Run a refresh with
`python -m ingest.run sales` (or `python -m ingest.daily` for the full nightly job).
Source API credentials live in `.env` (git-ignored) — never print or commit them.

## Core tables
- `sales` — one row per receipt/check. Key cols: `business_location_id`, `account_reference` (PK),
  `nb_covers` (int), `dine_in`, `cancelled`, `time_opening`/`time_closed` (timestamptz),
  `business_date` (date), `raw` (jsonb full payload). Pulling `raw` is large — select only what you need.
- `sales_lines` — line items (units, revenue) joined to the items catalogue.

## Key views (prefer these over raw tables)
- `v_report_lines` — line-level facts: `business_location_id, site, account_reference, sku, name,
  quantity, net_ex_vat, net_inc_vat, accounting_group_name, wet_dry, business_date, shift,
  item_category, line_staff`. The workhorse for sales/product reporting.
- `v_bookings` — one row per Favourite Table booking: `site, business_location_id, booking_date,
  dow_iso (1=Mon..7=Sun), booking_hour, covers, status, sale_channel_code, channel, showed,
  is_noshow, is_cancelled, is_live`. **Walk-ins = `sale_channel_code = 5`.**
- `v_bookings_site_day`, `v_unified_site_day` — bookings × sales per site×day (covers, no-show/cancel
  rates, spend per cover).
- `v_booking_pace`, `v_booking_pace_dow` — pre-booked vs same-day ("uplift") covers + weekday forecast shape.
- `v_void_lines`, `v_leakage_lines` — voids and discounts (leakage) line-by-line.
- `v_sentiment_overview` — review scores/volume.
- Staff/perf: `views_staff.sql`, `views_scorecard.sql`, `views_eotw.sql`, `views_plates.sql`,
  `views_basket.sql`, `views_nory.sql` (definitions in `db/`).

## Sites (group 100055)
Five Tap & Tandoor sites. `site`/`nickname` use the form **`Tap <Site>`**; note **Bournemouth's
nickname has a trailing dot: `Tap Bournemouth.`**. business_location_id map:
- 1718940401139714 — Bournemouth
- 1718940401139717 — Southampton
- 1718940401139718 — Portsmouth
- 1718940401139719 — Peterborough
- 1718940401139720 — Solihull

## Important data caveats
- **Covers are noisy:** `sales.nb_covers` is ~37% zeros (bar tabs, drinks-only, takeaway, transfers).
  For dine-in covers use `dine_in = true` and `nb_covers > 0`. Do NOT filter `cancelled = false`
  (it's null on normal checks and wipes all rows).
- **Not all sites live in `sales` yet:** only Bournemouth (714), Portsmouth (718), Peterborough (719)
  reliably appear; Solihull (720) and Southampton (717) sales may be absent/partial. Favourite Table
  `v_bookings` covers all 5 sites, so for cross-site cover/booking analysis prefer FT data and note the caveat.
- **Shifts:** Lunch = 12:00–16:59, Dinner = 17:00–21:59 (use `booking_hour < 17` = Lunch, else Dinner;
  or the `shift` column on `v_report_lines`).
- **Modifier pricing quirk:** some base items ring at £0 with the price on a dot-prefixed modifier line;
  match modifiers by name, not SKU.
- Example `item_category` values: `poppadoms`, `241 cocktails`, `desserts`, `loaded chips`,
  `croquettes`, `lunch menu`.

## Conventions
- Money is GBP. `net_ex_vat` = ex-VAT, `net_inc_vat` = inc-VAT.
- Default reporting window for "last night" = `business_date = current_date - 1`.
- When in doubt about a view's columns, read its definition in `db/views*.sql`.
