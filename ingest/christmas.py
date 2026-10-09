"""Christmas bookings: forward-pull December, then snapshot the totals.

Why this exists
---------------
The nightly bookings pull only looks BACK (today minus 14 days). December
dine dates are in the future, so they never reach Neon. This step:

  1. pulls every dine date 1-31 December from Favourite Table (reusing
     ingest.bookings, so decode maps, Darts handling and the upsert are the
     same code path as everything else), then
  2. writes one row per site into `christmas_snapshots` for today.

Why a snapshot table
--------------------
"Up or down on yesterday" cannot be rebuilt from the bookings table: FT does
not say WHEN a booking was cancelled, so a booking made last week and
cancelled this morning would be invisible in a reconstruction. Taking a
snapshot each morning and diffing the two latest is the only honest answer.
The notify layer is read-only on purpose, so the snapshot is written HERE.

Counted: every booking with a dine date 1-31 December that is not Cancelled.
Guests = FT GuestCount. Bournemouth Darts is folded into Bournemouth.

Run:
    python -m ingest.christmas                  # pull December + snapshot
    python -m ingest.christmas --snapshot-only  # snapshot from what's in Neon
    python -m ingest.christmas --year 2026

Schedule: after the 09:30 bookings refresh, before the 11:00 message.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

import psycopg2

from . import bookings as bookings_mod
from .config import settings

log = logging.getLogger("ingest.christmas")

DDL = """
CREATE TABLE IF NOT EXISTS christmas_snapshots (
    snap_date  date        NOT NULL,   -- London date the snapshot was taken
    site_name  text        NOT NULL,   -- reporting site (Darts folded into Bournemouth)
    bookings   integer     NOT NULL,
    guests     integer     NOT NULL,
    taken_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (snap_date, site_name)
);
"""

# One row per reporting site (so a site with zero bookings still appears),
# counting every non-cancelled booking dined 1-31 December.
SNAPSHOT = """
INSERT INTO christmas_snapshots (snap_date, site_name, bookings, guests, taken_at)
SELECT (now() AT TIME ZONE 'Europe/London')::date,
       s.site_name,
       count(b.booking_ref_no),
       COALESCE(sum(b.guest_count), 0),
       now()
FROM (SELECT DISTINCT CASE WHEN site_name = 'Bournemouth (Darts)'
                           THEN 'Bournemouth' ELSE site_name END AS site_name
        FROM ft_site_map) s
LEFT JOIN bookings b
       ON CASE WHEN b.site_name = 'Bournemouth (Darts)'
               THEN 'Bournemouth' ELSE b.site_name END = s.site_name
      AND b.booking_date BETWEEN %(start)s AND %(end)s
      AND b.status IS DISTINCT FROM 'Cancelled'
GROUP BY s.site_name
ON CONFLICT (snap_date, site_name) DO UPDATE
   SET bookings = EXCLUDED.bookings, guests = EXCLUDED.guests, taken_at = now()
"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Pull December bookings and snapshot the totals")
    p.add_argument("--year", type=int, default=date.today().year)
    p.add_argument("--snapshot-only", action="store_true",
                   help="skip the Favourite Table pull; snapshot what is already in Neon")
    args = p.parse_args(argv)

    start, end = date(args.year, 12, 1), date(args.year, 12, 31)

    if not args.snapshot_only:
        if not settings.ft_auth_token:
            log.warning("FT_AUTH_TOKEN not set -- skipping Christmas pull.")
            return 0
        rc = bookings_mod.main(["--from", start.isoformat(), "--to", end.isoformat()])
        if rc:
            log.error("December pull failed (rc=%s) -- NOT snapshotting stale data.", rc)
            return rc

    conn = psycopg2.connect(settings.database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute(SNAPSHOT, {"start": start, "end": end})
            cur.execute("SELECT site_name, bookings, guests FROM christmas_snapshots "
                        "WHERE snap_date = (now() AT TIME ZONE 'Europe/London')::date "
                        "ORDER BY site_name")
            for site, bk, gu in cur.fetchall():
                log.info("  %-14s %4d bookings %5d guests", site, bk, gu)
        conn.commit()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
