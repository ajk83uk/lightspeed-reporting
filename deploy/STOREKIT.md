# StoreKit online orders — deploy & wire-up

StoreKit pushes order events to an **always-on** web service (the nightly cron
can't receive pushes). So this adds a *second* Railway service in the same
project, from the same repo, sharing the same Neon `DATABASE_URL`. The existing
cron worker is untouched.

```
StoreKit (5 stores) --Svix signed POST--> Railway web service
   ingest/storekit_webhook.py (Flask/gunicorn)
   -> Neon: storekit_orders (+ storekit_site_map, storekit_webhook_events)
   -> Metabase: v_storekit_orders_daily / v_storekit_orders_weekly
```

## One-time order of operations

1. **Commit & push** (run these yourself — the sandbox can't git on the OneDrive mount):
   ```bash
   cd "<repo>/lightspeed-reporting"
   git add db/storekit_schema.sql db/views_storekit.sql ingest/storekit_webhook.py \
           ingest/config.py ingest/db.py ingest/migrate.py requirements.txt deploy/STOREKIT.md
   git commit -m "Add StoreKit online-orders webhook ingestion + daily/weekly views"
   git push
   ```

2. **Apply the schema + views** to Neon (one-off, safe to re-run):
   ```bash
   railway run python -m ingest.migrate --no-seed
   ```
   (`--no-seed` so it doesn't touch your category rules. This now also applies
   `storekit_schema.sql` and `views_storekit.sql`.)

3. **Create the web service** in the SAME Railway project:
   - New Service → Deploy from the same repo.
   - Settings → Start Command: `gunicorn ingest.storekit_webhook:app --bind 0.0.0.0:$PORT`
   - Variables: add `STOREKIT_WEBHOOK_SECRET` (filled in step 5) and the shared
     `DATABASE_URL` (reference the same Neon var the cron service uses).
   - Networking → Generate Domain. This gives you the public URL, e.g.
     `https://storekit-webhook-production.up.railway.app`.
   - Your endpoint is that domain + `/webhooks/storekit`.
   - Health check path (optional): `/health`.

   > Note: this service has NO `cronSchedule` — it's a long-running web process.
   > `railway.json` (cron) stays attached to the *worker* service only; set the
   > start command on the web service via the dashboard, not railway.json.

4. **Register the endpoint in StoreKit** — for EACH of the 5 stores:
   Dashboard → that store → Settings → Integrations → Webhooks → add endpoint
   `https://<your-domain>/webhooks/storekit`, subscribed to:
   `order.created`, `order.accepted`, `order.rejected`, `order.canceled`,
   `order.completed`, `order.refund.created`.
   (One endpoint serves all sites; `venue.id` in the payload tells them apart.)

5. **Copy the signing secret** StoreKit shows on setup into the Railway
   `STOREKIT_WEBHOOK_SECRET` variable, then redeploy the web service.

6. **Fire a test order** (or use StoreKit's "send test event"). Confirm a row
   lands:
   ```sql
   SELECT order_id, venue_id, venue_name, order_type, total_pence
   FROM storekit_orders ORDER BY first_seen_at DESC LIMIT 5;
   ```

7. **Seed the site map** with the real `venue.id` for each store (from the test
   rows above or the dashboard URL). Edit the template block in
   `db/storekit_schema.sql`, then re-run `railway run python -m ingest.migrate
   --no-seed`. Until this is seeded, the views fall back to `venue.name`, so
   numbers still work — the map just gives clean labels + the join to
   Lightspeed/Nory/FT via `business_location_id`.

## Local testing (no signature)

```bash
export STOREKIT_SKIP_VERIFY=1
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/lightspeed
python -m ingest.storekit_webhook       # serves on :8080
# in another shell, replay the sample order.created from the docs:
curl -X POST localhost:8080/webhooks/storekit -H 'content-type: application/json' \
  -H 'svix-id: test_1' -d '{"event":"order.created","data":{"id":"ord_test","code":"A1B2","total":2500,"tip":250,"deliveryFee":299,"discountTotal":0,"orderType":"Pickup","createdAt":"2026-06-30T10:30:00Z","venue":{"id":1234,"name":"Tap Solihull"},"items":[]}}'
```

## Metabase

Point new cards at the views:
- **Daily report:** `v_storekit_orders_daily` — `orders`, `aov`, `net_sales`,
  `tips`, `discounts`, channel split, filtered by `order_date` + `site`.
- **Weekly report:** `v_storekit_orders_weekly` — same metrics by `order_week`.

`net_sales` = total − tip − delivery (inc VAT). `net_sales_ex_vat` divides by
1.20 for like-for-like with the Lightspeed headline (which ranks on ex-VAT).
`gross_sales` is the full charged amount if you'd rather show that.
