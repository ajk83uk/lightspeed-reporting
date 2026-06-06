# Tap & Tandoor — Lightspeed Reporting

Self-hosted reporting for the five Tap & Tandoor sites. Pulls read-only data
from the Lightspeed K-Series API into Postgres, and surfaces it through
Metabase as filterable dashboards and scheduled (automated) reports.

## Architecture

```
Lightspeed K-Series API  ──(OAuth2, read-only)──►  Ingestion worker (Python)
   financial-api (V2 Sales)                              │  on a schedule
   items                                                 ▼
                                                    Postgres warehouse
                                          sites / items / sales / sales_lines /
                                          payments / category_rules + views
                                                          │
                                                          ▼
                                                       Metabase
                                   • filterable dashboards (date + site)
                                   • 1–5 site league tables
                                   • scheduled email/Slack reports
```

Why this shape: the API pages at 100 sales/request and needs token handling, so
we cache into Postgres rather than query it live. Metabase then gives both
deliverables — an interactive dashboard *and* scheduled reports — from one tool.

## Repository layout

| Path | What it is |
|------|-----------|
| `db/schema.sql` | Tables (sites, items, sales, sales_lines, payments, category_rules, ingest_state) |
| `db/views.sql` | Enrichment + categorisation views (`v_fact_lines`, `v_fact_payments`, etc.) |
| `db/seed_categories.sql` | Accounting groups + wet/dry + poppadom/cocktail/dessert rules |
| `db/discover_items.sql` | Helper queries to tune the category rules against your catalogue |
| `metabase/ranking_queries.sql` | The six 1–5 league-table queries + a combined scoreboard |
| `ingest/` | Python ingestion worker |
| `docker-compose.yml` | Local Postgres + Metabase |
| `Dockerfile` | Worker image for Railway/any container host |

## Data model notes (important)

**Wet/dry** is derived from accounting group: `Alcoholic Drinks` +
`Non-Alcoholic Drinks` → *wet*; `Food` → *dry*. `Misc`, `OA-AG` and
`Tap Accounting Group` are left as *other* — adjust in `db/seed_categories.sql`.

**Poppadoms / cocktails / desserts** sit *inside* those groups, so they're
matched by item-name patterns to start with. After your first ingest, run
`db/discover_items.sql` to see what the patterns catch, then tighten them to
explicit SKUs (`match_type='sku'`) for accuracy. This mapping table is the one
part that needs your domain knowledge; everything else is mechanical.

**Tips** come from `payments.tip`. **Headline sales** uses `net_ex_vat`
(ex-VAT); switch to `net_inc_vat` in the queries if you'd rather rank on gross.

## Quick start (local)

```bash
cp .env.example .env          # fill in Lightspeed creds + DB url
docker compose up -d          # Postgres on :5432, Metabase on :3000

# load schema, views, seed (psql or any client)
psql "postgresql://postgres:postgres@localhost:5432/lightspeed" \
  -f db/schema.sql -f db/views.sql -f db/seed_categories.sql

# one-time: get a refresh token (see OAuth below), put it in .env
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m ingest.get_refresh_token

# first backfill, then incrementals
python -m ingest.run items
python -m ingest.run sales --full
```

Then open Metabase at http://localhost:3000, connect it to the `lightspeed`
Postgres database, and build the dashboard (below).

## OAuth (one-time)

Lightspeed K-Series V2 uses the **authorization-code grant** on a dedicated auth
host (Keycloak). A few specifics that this code already handles for you:

- Auth host is separate from the API host: `auth.lsk-prod.app` (or
  `auth.lsk-demo.app` for trial). Clients are environment-bound.
- Your V2 `client_id` begins with `devp-v2-prod-` (or `devp-v2-demo-`).
- Credentials are sent as an HTTP **Basic Authorization header**, not in the
  request body (this is Lightspeed's most common integration mistake).
- The refresh token **rotates** on every use — the latest one is stored in the
  `oauth_token` DB table automatically, so the hourly cron keeps working.

Steps:

1. Get your `client_id` / `client_secret` and register a redirect URI
   (e.g. `https://localhost/`). Put the id/secret in `.env`.
2. Make sure the DB exists: `python -m ingest.migrate`.
3. Run `python -m ingest.get_refresh_token`, open the printed URL, log in with
   your backoffice POS admin credentials, approve, and paste back the `code`
   (or the whole redirect URL). It stores the refresh token in the DB.
4. From then on the worker auto-refreshes; no further manual steps.

Scopes requested: `financial-api items offline_access` (the first two are the
read-only data scopes; `offline_access` extends the refresh-token lifetime).

## Building the dashboard in Metabase

1. **Admin → Databases → Add** the `lightspeed` Postgres.
2. For each query in `metabase/ranking_queries.sql`: **+ New → SQL query**,
   paste, set the `{{start}}` / `{{end}}` variables to **Date**, save. (Or just
   use the single "combined scoreboard" query for one tile with every rank.)
3. **+ New → Dashboard**, add the saved questions.
4. Add a **Date** filter to the dashboard and wire it to `{{start}}/{{end}}` on
   every card — one control then drives all the league tables. Add a **Site**
   filter the same way if you want to drill into one site.

## Automated reports

In Metabase, open the dashboard → **Subscriptions** (the sharing/clock icon) →
set a schedule (e.g. Monday 7am) and recipients (email, or Slack if connected).
That *is* your automated report — same data, on a timer. Use **Alerts** on a
question if you'd rather be pinged only when a value crosses a threshold.

## Production hosting (Railway — recommended)

1. New Railway project → add a **Postgres** plugin (managed, backed up).
2. Load `schema.sql` + `views.sql` + `seed_categories.sql` into it once.
3. Deploy this repo as a **service** from the `Dockerfile`. Set the env vars
   from `.env` (use `DATABASE_URL` from the Postgres plugin).
4. Add two **cron** schedules on the service:
   - `python -m ingest.run items`  — nightly (e.g. `0 3 * * *`)
   - `python -m ingest.run sales`  — hourly (e.g. `0 * * * *`)
5. Deploy **Metabase** (its Docker image) as another service, or use
   **Metabase Cloud** and point it at the Railway Postgres. Create user
   accounts/groups for your managers; scope per-site access if wanted.

## Things to VERIFY against the spec before first run

The OAuth hosts and scopes are confirmed (per Lightspeed's OAuth quick-start).
The data endpoint **paths** still have sensible defaults but should be confirmed
against the machine-readable spec at
`https://api-docs.lsk.lightspeed.app/source.yaml`:

- `LS_PATH_BUSINESSES` — the "Get Businesses" route.
- `LS_PATH_SALES` — FinancialV2 "Get Sales" route.
- `LS_PATH_ITEMS` — "Get All Items" route.

The JSON field mapping lives entirely in `ingest/db.py` (`_map_*` functions),
so if a field name differs in your tenant, fix it there in one place. Every
table also stores the full raw payload in a `raw` JSONB column, so nothing is
lost if a mapping needs revisiting.
