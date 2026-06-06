"""Ingestion entry point.

Usage:
    python -m ingest.run items          # refresh catalogue for all sites
    python -m ingest.run sales          # incremental sales (uses watermark)
    python -m ingest.run sales --full   # full backfill (LS_BACKFILL_DAYS)
    python -m ingest.run all            # items then incremental sales

Designed to be run on a schedule (e.g. Railway cron): 'items' nightly,
'sales' hourly.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from .client import LightspeedClient
from .config import settings
from . import db

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("ingest")


def _loc_id(loc: dict) -> int | None:
    # The location-id key spelling varies; try the documented + common variants.
    for key in ("blId", "blID", "blid", "businessLocationId", "businessLocationID", "id"):
        v = loc.get(key)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return None


def discover_sites(conn, client: LightspeedClient) -> list[int]:
    blids: list[int] = []
    targets = settings.business_ids  # empty = no filter
    for loc in client.iter_business_locations():
        if targets and loc.get("businessId") not in targets:
            continue  # skip other head offices (old 69798, Zindiya, etc.)
        blid = _loc_id(loc)
        name = loc.get("blName") or loc.get("name")
        if blid is None:
            log.warning("No location id found. Raw keys=%s  data=%s",
                        list(loc.keys()), loc)
            continue
        # Seed business_name and nickname with the location name so dashboards
        # have a label; you can rename the nickname later.
        db.upsert_site(conn, blid, name, name)
        blids.append(blid)
        log.info("  location %s -> %s (business %s)", blid, name, loc.get("businessId"))
    conn.commit()
    log.info("Discovered %d business location(s): %s", len(blids), blids)
    return blids


def ingest_items(conn, client: LightspeedClient, blids: list[int]) -> None:
    for blid in blids:
        items = list(client.iter_items(blid))
        n = db.upsert_items(conn, blid, items)
        conn.commit()
        log.info("[%s] items upserted: %d", blid, n)


def ingest_sales(conn, client: LightspeedClient, blids: list[int], full: bool,
                 days: int | None = None) -> None:
    now = datetime.now(timezone.utc)
    MAX_WINDOW = 364  # Lightspeed rejects "from→to" ranges of ~365 days with a 400
    for blid in blids:
        try:
            if full or days:
                window = min(days if days else settings.backfill_days, MAX_WINDOW)
                frm_dt = now - timedelta(days=window)
            else:
                wm = db.get_watermark(conn, blid, "sales")
                frm_dt = (wm - timedelta(hours=settings.incremental_overlap_hours)) if wm \
                    else now - timedelta(days=min(settings.backfill_days, MAX_WINDOW))
            # Hard cap so the requested window can never exceed the API limit.
            if now - frm_dt > timedelta(days=MAX_WINDOW):
                frm_dt = now - timedelta(days=MAX_WINDOW)
            frm = frm_dt.astimezone(timezone.utc).isoformat()
            to = now.isoformat()

            batch, total, max_closed = [], 0, None
            for sale in client.iter_sales(blid, frm, to):
                batch.append(sale)
                tc = sale.get("timeClosed") or sale.get("timeofCloseAndPaid")
                if tc and (max_closed is None or tc > max_closed):
                    max_closed = tc
                if len(batch) >= 500:
                    s, l, p = db.upsert_sales_batch(conn, blid, batch)
                    conn.commit()
                    total += s
                    log.info("[%s] +%d sales (%d lines, %d payments)", blid, s, l, p)
                    batch = []
            if batch:
                s, l, p = db.upsert_sales_batch(conn, blid, batch)
                conn.commit()
                total += s
                log.info("[%s] +%d sales (%d lines, %d payments)", blid, s, l, p)

            wm_dt = db._dt(max_closed)
            if wm_dt:
                db.set_watermark(conn, blid, "sales", wm_dt)
                conn.commit()
            log.info("[%s] sales ingest complete: %d sales, watermark=%s",
                     blid, total, wm_dt)
        except Exception as exc:  # one site failing must not abort the others
            conn.rollback()
            log.error("[%s] sales ingest FAILED, skipping: %s", blid, exc)
            continue


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lightspeed ingestion")
    parser.add_argument("mode", choices=["items", "sales", "all", "businesses"])
    parser.add_argument("--full", action="store_true", help="full backfill for sales")
    parser.add_argument("--days", type=int, default=None,
                        help="sales: pull this many days back (overrides default window)")
    args = parser.parse_args(argv)

    client = LightspeedClient()
    conn = db.connect()
    try:
        blids = discover_sites(conn, client)
        if not blids:
            log.error("No business locations found; aborting.")
            return 1
        if args.mode == "businesses":
            log.info("Connection can see %d business location(s) -- listed above.", len(blids))
            return 0
        if args.mode in ("items", "all"):
            ingest_items(conn, client, blids)
        if args.mode in ("sales", "all"):
            ingest_sales(conn, client, blids, full=args.full, days=args.days)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
