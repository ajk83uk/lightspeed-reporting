# notify/ — setup, and adding messages later

Two parts: the **one-time deploy**, then **how to add a message** to any chat at
any time without touching Railway again.

---

# Part 1 — One-time setup (~10 minutes)

## 1. Push the code

From the repo folder in PowerShell:

```powershell
cd "C:\Users\ajayk\OneDrive\Documents\Claude\Projects\Lightspeed API\lightspeed-reporting"
git status
```

Read the list before committing. You should see `notify/`, `railway.brief.json`,
and changes to `Dockerfile`, `requirements.txt`, `CLAUDE.md`.

You should **not** see `.env` or `gcp-cashoff-key.json` — both are gitignored.
If either appears, stop and say so.

```powershell
git add -A
git commit -m "notify: daily site brief and labour alerts to ZenZap"
git push
```

## 2. Create the Railway service

Railway → your project → **New** → **GitHub Repo** → `lightspeed-reporting`
(the same repo the ingest service uses — Railway is fine with several services
from one repo).

Then on the new service:

- **Settings → Config as code** → set the path to `railway.brief.json`
- **Settings → Name** → something like `notify`, so it's obvious in the list

## 3. Add four variables

**Variables** tab → add:

| Variable | Where it comes from |
|---|---|
| `DATABASE_URL` | copy from the existing ingest service — identical value |
| `ZENZAP_API_KEY` | app.zenzap.co → Apps & Agents → TAPPY |
| `ZENZAP_API_SECRET` | same screen |
| `ZENZAP_BASE_URL` | `https://api.zenzap.co` |

Watch for a trailing newline when pasting `DATABASE_URL` — it shows up later as
`invalid sslmode value: "require\n"`. The code strips it, but paste cleanly.

## 4. Deploy and check

Railway deploys on push. Open the service's **Deployments → View logs**.

The service wakes **every hour**. Most hours it prints:

```
Thu 20 Aug 14:00 — no rules due this hour.
```

That is correct and healthy — it means the scheduler works and nothing was due.

At 09:00 and 11:00 you should see real work:

```
Thu 20 Aug 11:00 — due now: ['daily-site-brief']
=== daily-site-brief — Morning brief — last night, then today ===
  5 rows, 5 to send
  ✓ Tap & Tandoor Bournemouth
  ...
1 rule(s): 5 sent, 0 failed
```

## 5. Turn off the old Cowork task

In the Claude app sidebar, delete or leave disabled `tt-daily-site-brief`.
It cannot work (scheduled runs get a fresh session with no access to the code)
and leaving it enabled risks a second message if that ever changes.

**Done.** You never need to touch Railway again to add messages.

---

# Part 2 — Adding a message

Two files, depending on what you're adding:

| You want | Edit |
|---|---|
| "Remind Solihull to clean the lines every Tuesday" | `notify/config/reminders.yaml` |
| "Tell a site when covers drop 25% on last week" | `notify/config/alerts.yaml` |

A **reminder** fires on a schedule and says something. No data involved.
An **alert** queries Neon and only fires when the numbers say so.

Both: commit, push, live from the next matching hour. No Railway change ever.

---

## Reminders — "do X at this time on this day"

This is the simple one. Open `notify/config/reminders.yaml` and add:

```yaml
  - key: friday-cellar-check
    enabled: true
    title: Friday cellar check
    schedule: "0 15 * * 5"        # Fridays 15:00 UK
    route: site                   # each site's own chat
    message: |
      *Friday cellar check*
      Gas and line check before the weekend. Confirm here once done.
```

That's the whole thing. To send it to **one site**, add:

```yaml
    sites: [tt-solihull]
```

Site keys: `tt-solihull`, `tt-peterborough`, `tt-portsmouth`,
`tt-bournemouth`, `tt-southampton`. Omit `sites:` for all of them.

Test it before it goes anywhere:

```bash
python -m notify.brief --rule friday-cellar-check --dry-run
```

Three worked examples ship in `reminders.yaml`, all `enabled: false` — copy
the closest one rather than starting from scratch.

**Just tell me the schedule in plain English and I'll write these for you** —
"remind Portsmouth every Monday at 9 to do the fridge temps" is enough.

---

## The live-sport block

The morning brief ends with today's fixtures and channels. It is on because
`daily-site-brief` carries `sport_block: true` in `alerts.yaml`; delete that
line and the brief goes back to numbers only.

Both halves come from FANZO (`notify/sport.py`):

| Call | Gives us |
|---|---|
| venue 17079 `widget-json` | which fixtures we're showing (~4 days ahead) |
| each fixture's `bars-showing` page | which channel |

They join on FANZO's numeric fixture id. Things worth knowing:

- **All five sites share venue 17079**, so every site's brief gets the same
  list. If sites ever get their own FANZO venues, add the ids and fetch per site.
- **A fixture only appears if it's in FANZO.** If a game isn't on the Live
  Sports tab of the website, it won't be in the message either. That's a
  dashboard job, not a code one.
- **Don't move the channel lookup to a TV-listings site.** It was built that
  way first. Matching fixtures across two vendors means matching on team name,
  the names disagree ("Bradford" vs "Bradford City"), and any rule that fixes
  that also merges "Manchester United" with "Manchester City". Matching on
  FANZO's own fixture id removes the problem instead of guarding against it —
  and covers rugby, F1 and cricket, which football-only listings don't.
- **It fails soft.** If either call breaks, the sport block is empty and the
  brief sends as normal.

Preview any day:

```bash
python -c "from notify import sport; print(sport.block())"
```

---

## Alerts — data-driven

Everything below is `notify/config/alerts.yaml`.

## The shape of a rule

```yaml
  - key: covers-well-down          # unique; also the idempotency key
    enabled: true
    title: Covers well down on last week
    schedule: "0 16 * * *"         # 16:00 UK — see Timing below
    description: >
      Why this exists and what it's for. Read by whoever inherits this.
    sql: |
      SELECT ...                   -- must return one row per site
    condition:
      all:
        - column: covers_vs_lw_pct
          op: "<="
          value: -25
    message: |
      *Covers down — {site_display}*
      {covers} last night vs {covers_lw} the same day last week.
    route: site
```

## Timing — "at certain times"

`schedule` is standard 5-field cron, evaluated in **UK local time**, so it
follows the clocks by itself. The service runs hourly, so the **hour** and
**day** are what matter; the minute is ignored.

| You want | `schedule:` |
|---|---|
| Every day at 11:00 | `0 11 * * *` |
| Every day at 16:00 | `0 16 * * *` |
| Mondays at 09:00 | `0 9 * * 1` |
| Weekdays at 08:00 | `0 8 * * 1-5` |
| Sundays at 20:00 | `0 20 * * 0` |
| Mon + Thu at 10:00 | `0 10 * * 1,4` |

Days: `0` or `7` = Sunday, `1` = Monday … `6` = Saturday.

Several rules can share an hour — they all run, in file order.

**Leave `schedule:` out and the rule never fires automatically.** That's the
safe way to park something you're still drafting: it stays runnable by hand
with `--rule` but can't surprise anyone.

## Destination — "to certain chats"

| `route:` | Goes to |
|---|---|
| `site` | each site's own chat — Solihull's row to Solihull, etc. |
| `group:south_coast` | the South Coast Sites group |
| `group:announcements` | Group Announcements — **think hard first** |
| `none` | nothing is sent, only printed in the Railway log |

`site` is the normal choice. Use `group:` when the message genuinely concerns
everyone in that group at once; a per-site message posted to a shared group is
four sites' noise for one site's benefit.

`none` is for data-quality checks — things nobody at site level could act on.
It still runs and still logs, it just doesn't message anyone.

Groups are defined in `notify/config/sites.yaml`. To add one, add TAPPY to the
ZenZap group, regenerate the topic lockfile, and add a `groups:` entry.

## Writing the SQL

One row per site. Return `location_id` wherever you can:

```sql
SELECT sd.business_location_id::text AS location_id,
       sd.business_date,
       ...
FROM v_site_day sd
WHERE sd.business_date = CURRENT_DATE - 1
```

Site **names** differ between sources — `Solihull` in Nory and bookings,
`Tap Solihull` in the POS views, and `Tap Bournemouth.` carries a trailing full
stop. The numeric id is the only handle that means the same thing everywhere.

Anything the query returns is available in `message` as `{column_name}`.
`{column:date}` renders `2026-08-19` as `Wed 19 Aug`.

## Conditions

```yaml
condition:
  all:                      # every clause must be true
    - column: hours_var
      op: ">="              # > >= < <= == !=
      value: 2.0
```

Or for something that always sends:

```yaml
condition: always           # a brief, not an alert
```

A row whose column is `NULL` **never** matches. Missing data mustn't read as a
breach, and mustn't be quietly treated as zero.

## Test before it goes anywhere near a staff group

```bash
python -m notify.brief --rule covers-well-down --dry-run
```

Prints the exact message and destination, sends nothing, ignores the schedule.
Check:

- the numbers look right, and any arithmetic in the message ties
- no `None`, no empty value after a label, no leftover `{placeholder}`
- it fires for the sites you'd expect and stays quiet for the rest

Then run it for real once, off-schedule:

```bash
python -m notify.brief --rule covers-well-down
```

Happy? Commit and push. It's live from the next matching hour.

## Two habits worth keeping

**Check the volume before you enable it.** Run the condition against a couple
of weeks of history first. A rule that fires most days for most sites gets
muted within a fortnight and takes the useful messages with it — people stop
reading the source, not the individual rule.

**Pair criticism with praise on the same bar.** `labour-hours-over` and
`labour-hours-saved` use identical thresholds, inverted, and a test enforces
it. A system that only ever messages people when something's wrong reads as
nagging and quietly stops working.

---

# Safety rails you get for free

- **Idempotent.** Every message carries `{rule}-{YYYYMMDD}-{site}` as its
  ZenZap `externalId`. A re-run on the same day cannot double-post, so a late
  or repeated run is always safe.
- **Send cap.** More than 5 sites matching aborts the whole run. Five sites
  breaching at once is nearly always a broken filter.
- **Quiet hours.** Per site, from `sites.yaml`. These land on personal phones.
- **Fails closed.** Any doubt about which site a row belongs to and it's
  skipped and reported, never guessed.
- **Templates fail loudly.** A message referencing a column the query doesn't
  return raises rather than posting `{covers}` into a staff group.

# When something goes wrong

Railway → the `notify` service → **Deployments → View logs**.

| Log says | Cause |
|---|---|
| `no rules due this hour` | normal, most hours |
| `401` from ZenZap | wrong secret, or a trailing space in it |
| `invalid sslmode value: "require\n"` | newline in `DATABASE_URL` |
| `not in sites.yaml` | a ZenZap group was renamed — regenerate the lockfile |
| `REFUSING TO SEND` | too many matches; check the query before overriding |
| `references {x} which the query doesn't return` | typo between SQL and message |
