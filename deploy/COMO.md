# Como loyalty webhooks -> Neon -> Metabase

Receiver: `ingest/como_webhook.py` (own always-on Railway web service, `railway.como.json`).
Mapping + SQL: `ingest/como.py`. Tables: `db/como_schema.sql`. Views: `db/views_como.sql`.

## Safety defaults
- **Switched off**: with `COMO_ENABLED` unset the receiver only logs `COMO DRYRUN ...` and returns 200. Nothing is written.
- **Fail closed**: no `COMO_WEBHOOK_TOKEN` -> every request 404. Como documents no signature scheme, so the secret token in the URL path *is* the auth.
- **No PII stored**: names / email / phone are scrubbed from every stored JSON blob; only salted SHA-256 hashes (`COMO_HASH_SALT`) are kept for matching to Favourite Table.

## Railway env vars (new service)
| Var | Value |
|---|---|
| `DATABASE_URL` | same Neon DB as the other services |
| `COMO_WEBHOOK_TOKEN` | long random string (>= 32 chars). No spaces/newlines. |
| `COMO_HASH_SALT` | long random string. **Never change once set** (hashes would stop matching). |
| `COMO_ENABLED` | leave unset for dry-run; `1` to go live |

## Endpoint to give Como
`https://<service-domain>.up.railway.app/webhooks/como/<COMO_WEBHOOK_TOKEN>`
Health (no secrets): `/health`.

## Rollout order
1. Push repo, create Railway service (`railway.como.json`), set env vars (dry-run).
2. Apply DB: `python -m ingest.apply_sql db/como_schema.sql db/seed_como_map.sql db/views_como.sql db/grants_como.sql`
3. In the Como Hub create the Export Event rule(s) pointing at the endpoint. Set throughput low (e.g. 5).
4. Trigger a test; check Railway logs for `COMO DRYRUN`.
5. Check the branch id and timestamps in the dry-run line (see below), then set `COMO_ENABLED=1`.

## Verify on the first live events
- `SELECT branch_id, COUNT(*) FROM como_events GROUP BY 1;` -> add any BranchID not in `como_site_map`.
- `como_transactions.transaction_id` vs Lightspeed check ref: does it match? (decides check-level APC join)
- `TotalSum`: gross or net of VAT? Compare to the Lightspeed check.
- `purchased_at` vs the real till time (epoch is treated as UTC).
- Does `joinedclub` carry any branch? (sign-ups per site currently attributed to first-purchase site.)
- `sites.como_branch_id` is added by the schema but not yet populated.

## Known limits
- Forward-only (no history).
- No void/cancel event is documented, so a voided loyalty purchase would still count.
- `PointTransaction`, `punch`, `tag_operation`, `openedclub`, `smsunsubscribe` are stored in `como_events` (scrubbed) but not yet modelled.
