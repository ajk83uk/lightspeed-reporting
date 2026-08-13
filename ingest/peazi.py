"""Peazi order-line ingestion (Saint Pauls Market stall -> Postgres).

Pulls line-level transactions from Peazi's own console backend
(transactionsReport on Google Cloud Functions; plain GET, no auth header --
the console sends the same requests). Mirrors nory/bookings: env-guarded
clean skip, rolling window re-pull so refunds/late orders self-heal.

Grain: one row per (order_number, plu). Duplicate PLU lines within an order
are aggregated before upsert (the endpoint can emit them separately).

Run:
    python -m ingest.peazi                    # rolling window (PEAZI_WINDOW_DAYS, default 7)
    python -m ingest.peazi --days 365         # backfill
    python -m ingest.peazi --from 2025-07-01 --to 2026-07-06
    python -m ingest.peazi --dry-run

Env (set in Railway on the Zindiya service):
    PEAZI_SITE          site slug, e.g. saintpaulsmarket   (REQUIRED; skips if unset)
    PEAZI_USER_ID       console user id used by the report endpoints (REQUIRED)
    PEAZI_LABELS        comma-separated reportingLabel UUIDs to filter (optional)
    PEAZI_WINDOW_DAYS   default 7
    PEAZI_THROTTLE_SECS default 0.2 (between pages)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta

import psycopg2
import psycopg2.extras
import requests

from .config import settings

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("ingest.peazi")

BASE = "https://europe-west1-peazi-production.cloudfunctions.net"
PAGE_SIZE = 500
# The console always sends the full payment-method list; mirror it so totals
# match what the console shows.
PAY_METHODS = ["card", "kiosk", "terminal", "gift card", "multiple", "Band",
               "Make it Right", "Management", "Marketing", "Influencer",
               "Complimentary"]


def _params(frm: date, to: date, page: int) -> list[tuple]:
    p = [("site", settings.peazi_site), ("tradingDateFrom", frm.isoformat()),
         ("tradingDateTo", to.isoformat()), ("userId", settings.peazi_user_id),
         ("pageSize", str(PAGE_SIZE)), ("page", str(page))]
    p += [("reportingLabels[]", l) for l in settings.peazi_labels_list]
    p += [("paymentMethods[]", m) for m in PAY_METHODS]
    return p


def iter_lines(session, frm: date, to: date):
    page = 0
    while True:
        for attempt in range(4):
            resp = session.get(f"{BASE}/transactionsReport",
                               params=_params(frm, to, page),
                               timeout=settings.http_timeout)
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 20))
                continue
            resp.raise_for_status()
            break
        body = resp.json()
        data = body.get("data") or []
        yield page, body.get("totalCount"), data
        if len(data) < PAGE_SIZE:
            return
        page += 1
        if settings.peazi_throttle_secs:
            time.sleep(settings.peazi_throttle_secs)


_COLS = ["site", "order_number", "plu", "order_time", "name", "quantity", "price",
         "total", "discount_amount", "tip", "charge", "commission", "payout",
         "payment_method", "reporting_labels"]
_UPSERT = f"""
INSERT INTO peazi_order_lines ({','.join(_COLS)}, updated_at)
VALUES ({','.join(['%s'] * len(_COLS))}, now())
ON CONFLICT (site, order_number, plu) DO UPDATE SET
  {', '.join(f'{c}=EXCLUDED.{c}' for c in _COLS if c not in ('site', 'order_number', 'plu'))},
  updated_at=now();
"""


def _rows(data: list[dict]) -> list[tuple]:
    # aggregate duplicate (order, plu) lines within the page batch
    agg: dict[tuple, list] = {}
    for r in data:
        key = (r.get("order_number"), int(r.get("plu") or 0))
        row = agg.get(key)
        if row is None:
            agg[key] = [settings.peazi_site, key[0], key[1], r.get("order_time"),
                        r.get("name"), r.get("quantity") or 0, r.get("price"),
                        r.get("total") or 0, r.get("discount_amount") or 0,
                        r.get("tip") or 0, r.get("charge") or 0,
                        r.get("commission") or 0, r.get("payout") or 0,
                        r.get("payment_method"),
                        ",".join(r.get("reportingLabels") or [])]
        else:
            row[5] += r.get("quantity") or 0
            row[7] += r.get("total") or 0
            row[8] += r.get("discount_amount") or 0
            row[9] += r.get("tip") or 0
            row[10] += r.get("charge") or 0
            row[11] += r.get("commission") or 0
            row[12] += r.get("payout") or 0
    return [tuple(v) for v in agg.values()]


def _resolve_dates(args) -> tuple[date, date]:
    today = date.today()
    if args.from_date or args.to_date:
        frm = datetime.strptime(args.from_date, "%Y-%m-%d").date() if args.from_date \
            else today - timedelta(days=settings.peazi_window_days)
        to = datetime.strptime(args.to_date, "%Y-%m-%d").date() if args.to_date else today
        return frm, to
    days = args.days if args.days is not None else settings.peazi_window_days
    return today - timedelta(days=days), today


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Peazi order lines -> Postgres")
    p.add_argument("--days", type=int)
    p.add_argument("--from", dest="from_date")
    p.add_argument("--to", dest="to_date")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    # Clean skip until configured, so the nightly ships dark (nory pattern).
    if not settings.peazi_site or not settings.peazi_user_id:
        log.warning("Peazi: PEAZI_SITE / PEAZI_USER_ID not set -- skipping Peazi ingest.")
        return 0

    frm, to = _resolve_dates(args)
    log.info("Peazi: pulling %s..%s for site %s", frm, to, settings.peazi_site)
    session = requests.Session()
    conn = None if args.dry_run else psycopg2.connect(settings.database_url)
    total = 0
    try:
        for page, total_count, data in iter_lines(session, frm, to):
            rows = _rows(data)
            if conn and rows:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_batch(cur, _UPSERT, rows, page_size=200)
                conn.commit()
            total += len(rows)
            log.info("  page %s: %d line(s)%s (report total %s)", page, len(rows),
                     " (dry-run)" if args.dry_run else "", total_count)
        log.info("Peazi ingest complete: %d line(s) upserted", total)
    finally:
        if conn:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
