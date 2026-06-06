# Railway deployment (nightly auto-pull)

Goal: the data refreshes itself every night with nothing running on your PC.

## What runs where

```
Neon Postgres        the warehouse (already set up, holds all data + the OAuth token)
Metabase Cloud       the dashboards (already set up, reads from Neon)
Railway              ONE cron service that runs the nightly pull, pointed at Neon
```

Railway does **not** need its own database. Neon already holds the schema, every
sale, the cash-off history, and the rotating Lightspeed refresh token. Railway is
just the machine that wakes up each night and runs the importer.

The single nightly job is `python -m ingest.daily`, which runs three things in
order and isolates them (one failing doesn't stop the others):

1. `items`   -- catalogue refresh
2. `sales`   -- incremental Lightspeed sales (watermark + 48h overlap)
3. `cashoff` -- the five Google Sheets cash-off forms -> `cashoff_daily`

---

## 1. Put the code on GitHub

From the `lightspeed-reporting` folder:

```bash
git init
git add .
git commit -m "Lightspeed reporting"
```

`.env` and `gcp-cashoff-key.json` are in `.gitignore`, so **no secrets get
committed** -- check `git status` shows neither before you push.

Create a new **private** repo on github.com, then:

```bash
git remote add origin https://github.com/<you>/lightspeed-reporting.git
git branch -M main
git push -u origin main
```

## 2. Create the Railway service

1. railway.com -> **New Project** -> **Deploy from GitHub repo** -> pick the repo.
   (Authorise Railway to see the repo if it asks.)
2. Railway detects the `Dockerfile` and builds it. The first deploy will just run
   the default help command -- that's fine, we set the real command next.

## 3. Set the environment variables

Service -> **Variables** -> add these. Only four are mandatory; the rest already
have correct defaults baked into the code.

| Variable | Value | Needed? |
|----------|-------|---------|
| `DATABASE_URL` | your **Neon** connection string (the same one in your local `.env`) | **yes** |
| `LS_CLIENT_ID` | your `devp-v2-prod-...` client id | **yes** |
| `LS_CLIENT_SECRET` | your client secret | **yes** |
| `GCP_KEY_JSON` | the **entire contents** of `gcp-cashoff-key.json`, pasted as one value | **yes** (for cash-off) |
| `LS_API_BASE` | `https://api.lsk.lightspeed.app` | optional (default ok) |
| `LS_BUSINESS_IDS` | `100055` | optional (default ok) |
| `LS_BACKFILL_DAYS` | `365` | optional |
| `LS_OVERLAP_HOURS` | `48` | optional |

Notes:
- **`DATABASE_URL`** points at Neon, not a Railway database. Copy it from your
  local `.env`. Use the *pooled* Neon connection string and keep `?sslmode=require`.
- **`GCP_KEY_JSON`** -- open `gcp-cashoff-key.json`, copy everything (the whole
  `{ ... }`), and paste it as the value. The code reads the key from this env var
  on the server, so the file itself never needs to leave your machine.
- **`LS_REFRESH_TOKEN` is deliberately NOT set.** The live token rotates on every
  refresh and already lives in Neon's `oauth_token` table; the job reads it from
  there. Setting a stale one in the env would only cause confusion.

## 4. Set the start command + nightly schedule

Service -> **Settings** -> **Deploy**:

- **Start Command:** `python -m ingest.daily`
- **Cron Schedule:** `0 3 * * *`  (03:00 UTC every day -- after the cash-off
  sheets are filled in for the night. Adjust the hour if you prefer.)

Railway cron notes: a run is skipped if the previous one is still going, and our
upserts are idempotent, so retries/overlaps are safe.

## 5. First run + check

1. Hit **Deploy** (or the cron's "Run now") once manually.
2. Open the **Logs**. You want to see, in order:
   `=== START items ===` ... `=== START sales ===` ... `=== START cashoff ===`
   and finally `Nightly ingest finished cleanly.`
3. Open a Metabase dashboard and confirm today's data is present.

That's it -- from here it refreshes itself every night. Nothing runs on your PC.

---

### If a step fails
The job exits non-zero and Railway marks the run failed, but **whatever
succeeded is already saved** (each step commits independently). The logs name the
failed step (`finished WITH FAILURES: cashoff`). Common causes:
- `cashoff` fails -> check `GCP_KEY_JSON` is the full JSON and the service account
  (`cash-off-importer@...`) is still shared on all five sheets.
- `sales`/`items` fail -> check `DATABASE_URL` reaches Neon and the Lightspeed
  client id/secret are right; the refresh token in `oauth_token` is still valid.

### Local runs still work unchanged
`python -m ingest.daily` (or the individual `python -m ingest.run sales` /
`python -m ingest.cashoff`) work the same on your PC, reading `.env` and the key
file. Railway just runs the same code on a timer.
