# feedback/ - guest feedback survey

Post-meal survey at feedback.tapandtandoor.co.uk. A guest who dined gets one email with a link that works once; the survey shows the dishes they had so they can rate them. **Ships switched off.**

Runs as its own always-on Railway service (`railway.feedback.json`, `gunicorn feedback.app:app`). The email job is `python -m feedback.send`.

| File | Purpose |
|---|---|
| `app.py` | Survey page, `GET /api/survey/<token>`, one-time `POST .../submit`, unsubscribe, `/health` |
| `send.py` | Daily email job. **Dry run by default.** Real sending needs `--live` AND `FEEDBACK_SEND_ENABLED=true` |
| `mailer.py` | Email template, dry-run provider, generic SMTP provider |
| `config.py` | Sites, phones, addresses, Google review links |
| `static/survey.html` | The branded survey |
| `tests/` | 22 tests; need a scratch Postgres (`FEEDBACK_TEST_DATABASE_URL`, schema in `tests/schema.sql`). Never run against Neon |

Schema and views: `db/feedback_schema.sql`, grants `db/grants_feedback.sql`. Head Office digest: rule `guest-feedback-daily` in `notify/config/alerts.yaml` (disabled).

## Rules
- Sender and reply-to are always `feedback@tapandtandoor.co.uk`; the site is in the display name and subject.
- One email per visit; never the same person twice in 14 days; unsubscribed addresses skipped; 300 a run.
- Link works once and expires 14 days after sending.
- Rating of 3 stars or below, or an allergy/illness/hygiene flag, creates a Head Office alert. Health alerts also go to Ally or Oppy.
- Dry-run rows (`dry_run = true`) never appear in reports. `python -m feedback.send --purge-dry-runs` clears them.
- This repo is PUBLIC: never commit keys. Provider credentials live only in Railway variables.

## Railway variables (service `guest-feedback`)
`DATABASE_URL`. Optional: `FEEDBACK_BASE_URL`, `FEEDBACK_SENDER`, `GREVIEW_*` overrides.
Left unset until the email provider is chosen: `FEEDBACK_SEND_ENABLED`, `FEEDBACK_PROVIDER` (`smtp`), `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`.

## Before anything sends
1. Pick the provider and add its DNS records (SPF, DKIM) for tapandtandoor.co.uk.
2. Create the `feedback@` alias of `info@`.
3. Registered company name and address in the email footer (`mailer.py`).
4. Point `feedback.tapandtandoor.co.uk` (CNAME) at the Railway service.
