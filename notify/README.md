# notify/ — outbound messages to staff

Sends the daily site brief and labour alerts into ZenZap. Reads from Neon;
never writes to it.

Runs as its **own Railway service** on cron. Nothing runs on a laptop, nothing
depends on a chat session being open, nothing breaks if a folder moves.

---

## Why this lives here and not in its own repo

It was originally built standalone and scheduled as a Cowork task. That failed
on the first unattended run: scheduled runs get a fresh session with no access
to the folder holding the code and credentials, so the job simply couldn't find
itself.

This repo already had everything the job needs — GitHub, Railway, a Docker
build, `DATABASE_URL`, and a working cron. Folding it in removed the whole
class of problem rather than working around it.

## Deploying (one-time)

1. Railway → **New Service** → same GitHub repo.
2. Settings → **Config as code** → `railway.brief.json`.
3. Variables — add these four:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | same as the ingest service |
   | `ZENZAP_API_KEY` | from app.zenzap.co → Apps & Agents |
   | `ZENZAP_API_SECRET` | as above |
   | `ZENZAP_BASE_URL` | `https://api.zenzap.co` |

   Paste carefully — a trailing newline in `DATABASE_URL` surfaces as
   `invalid sslmode value: "require\n"`. `notify/db.py` strips it, but the
   error is confusing if it slips through elsewhere.

4. Add a **second cron trigger** on the same service at `0 11 * * *`
   (the JSON sets `0 10 * * *`; both are needed — see Clocks).
5. Deploy, then check the logs for `5 sent, 0 failed`.

## Clocks

Railway cron is UTC and does not follow British Summer Time. A bare
`0 11 * * *` drifts to midday every October.

The service runs at **both 10:00 and 11:00 UTC**, and `brief.py` checks the
real `Europe/London` hour and exits quietly unless it is 11:00. Exactly one of
the two runs does the work, all year. `test_notify.py` pins this for both BST
and GMT — if someone "simplifies" it to a single cron, the test fails.

## Everyday use

```bash
python -m notify.brief --list                          # what's configured
python -m notify.brief --rule daily-site-brief --dry-run
python -m notify.brief --rule daily-site-brief --force # send now, off-schedule
```

`--dry-run` and `--rule` skip the clock guard; only unattended cron respects it.

## Adding an alert

Edit `notify/config/alerts.yaml`. No Python required.

```yaml
- key: covers-well-down
  enabled: true
  title: Covers well down on last week
  schedule: "0 11 * * *"
  sql: |
    SELECT business_location_id::text AS location_id, ...
  condition:
    all:
      - column: covers_vs_lw_pct
        op: "<="
        value: -25
  message: |
    *Covers down — {site_display}*
    ...
  route: site        # or: none  (print only, for data-quality checks)
```

Rules:

- Return `location_id` where you can — site **names** differ across sources
  (`Solihull` in Nory and bookings, `Tap Solihull` in the POS views, and
  `Tap Bournemouth.` carries a trailing full stop).
- `condition: always` makes it a brief — sends every day regardless.
- A row whose column is `NULL` never matches. Missing data must not read as a
  breach, and must not be silently treated as zero.
- A message referencing a column the query doesn't return raises rather than
  posting `{placeholder}` into a staff group.

## Safety rails

These exist because the audience is real staff in live groups.

- **Send cap.** More than `max_sites_per_run` (5) breaching aborts the run.
  Five sites all breaching at once is nearly always a broken query.
- **Idempotent.** Every message carries `{rule}-{YYYYMMDD}-{site}` as its
  ZenZap `externalId`, so a re-run cannot double-post. A late run is safe.
- **Quiet hours.** Per-site, from `sites.yaml`. These land on personal phones.
- **Fails closed.** Any doubt about which site a row belongs to, and it's
  skipped and reported rather than guessed.

## Config

| File | What it is |
|---|---|
| `config/sites.yaml` | the site registry — join keys, ZenZap group names, quiet hours |
| `config/alerts.yaml` | the rules |
| `config/topic_ids.yaml` | **generated** — ZenZap group name → topic UUID |

`topic_ids.yaml` is committed deliberately. ZenZap only accepts `externalId` at
topic *creation*, and these groups already existed, so names are the only
handle available — but matching on names at runtime means a renamed group could
route a site's numbers into another site's chat. Pinning the UUID turns a rename
into a loud failure instead. Regenerate with the standalone
`jobs/resolve_topics.py` if a group is renamed or added, and review the diff.

## Known data issues

- **Southampton and Peterborough report `sales = 0`** in `nory_labour_daily`
  every day. Their labour % is unusable, which is why `labour-pct-over-target`
  is disabled and `nory-sales-not-syncing` exists to flag it.
- **Nory's `planned_col` is a different cost basis** to `col` — £19.70–£25.68/hr
  planned against £14.76–£16.14 actual, almost certainly fully loaded with NI,
  pension and holiday. Subtracting them shows every site "saving" money every
  night. The brief derives £ from hours variance × the site's own actual rate
  instead, and a test fails if anyone reintroduces `planned_col`.
