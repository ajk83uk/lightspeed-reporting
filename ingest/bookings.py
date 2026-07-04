"""Favourite Table bookings ingestion (GetBookingList pull API -> Neon).

Pulls reservations per site per date and upserts one current-state row per
booking. Mirrors `ingest/nory.py`: a token-guarded clean skip until the env var
lands, and a rolling re-pulled window so late status changes self-heal.

Why a rolling window works: back-dated pulls return each booking's FINAL status
(confirmed Jaipal 2026-06-24), and BookingRefNo is unique + stable across status
changes, so re-pulling the last fortnight every night corrects any booking whose
status moved (Booked -> Show / NoShow / Complete) since we last saw it.

Run:
    python -m ingest.bookings                       # rolling window (FT_WINDOW_DAYS, default 14)
    python -m ingest.bookings --days 365            # last 12 months (one-off backfill)
    python -m ingest.bookings --from 2025-06-24 --to 2026-06-24
    python -m ingest.bookings --date 2026-06-23     # one specific dine date
    python -m ingest.bookings --site 2084           # restrict to one FT SiteCode

Env (set in Railway):
    FT_AUTH_TOKEN      production auth token (REQUIRED; step skips cleanly if unset)
    FT_BASE            default https://api.favouritetable.com
    FT_SITE_CODES      default 2084,2082,2102,2083,2086,2085  (Bournemouth = 2082 + 2102)
    FT_WINDOW_DAYS     default 14   (nightly rolling re-pull window)
    FT_THROTTLE_SECS   default 0.4  (gap between calls; FT throttles globally if load spikes)
    DATABASE_URL       reused from the existing config

Run `python -m ingest.migrate` first so the bookings + ft_site_map tables exist.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, time as dtime, timedelta

import psycopg2
import psycopg2.extras
import requests

from .config import settings

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("ingest.bookings")

# --- code -> label decode maps (from FT, see plan/memory) -------------------
STATUS = {1: "Booked", 2: "Confirmed", 3: "Cancelled", 4: "Show", 5: "NoShow", 6: "Complete"}
CHANNEL = {1: "Web", 2: "Phone", 5: "Walk-in", 103: "Third-party"}
INTERFACE = {1: "Google", 2: "Zomato", 3: "FTWaiting", 4: "Swiggy",
             5: "EasyDinner", 7: "RezControl", 8: "SquareMeal"}

# FT SiteCode -> reporting site label. 2102 (Darts & Shuffleboard) is its OWN
# reporting line, "Bournemouth (Darts)", split out of the main Bournemouth venue
# so its (often large, group) bookings don't inflate Bournemouth's headline.
# It shares the Bournemouth till, so its business_location_id is left NULL in
# ft_site_map -- sales stay on the main Bournemouth line, Darts is bookings-only.
SITE_NAMES = {
    2084: "Solihull", 2082: "Bournemouth", 2102: "Bournemouth (Darts)",
    2083: "Peterborough", 2086: "Portsmouth", 2085: "Southampton",
}


# --- small parsing helpers --------------------------------------------------
def _int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _bool(v):
    if isinstance(v, bool):
        return v
    if v in (1, "1", "true", "True"):
        return True
    if v in (0, "0", "false", "False"):
        return False
    return None


def _first(d: dict, *keys):
    """First present, non-empty value among several possible key spellings."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _secs_to_time(secs):
    """Seconds since midnight -> datetime.time (handles values >= 86400)."""
    s = _int(secs)
    if s is None:
        return None
    s %= 86400
    return dtime(s // 3600, (s % 3600) // 60, s % 60)


def _duration_mins(b: dict):
    """Prefer End-Start (both seconds since midnight); else fall back to Duration."""
    start = _int(_first(b, "BookingStartTime"))
    end = _int(_first(b, "BookingEndTime"))
    if start is not None and end is not None and end > start:
        return round((end - start) / 60)
    dur = _int(_first(b, "Duration"))
    if dur is None:
        return None
    # Duration is given in minutes for a normal sitting; if it looks like
    # seconds (implausibly large) convert it down.
    return round(dur / 60) if dur > 600 else dur


# --- HTTP -------------------------------------------------------------------
def _get_bookings(session, token: str, site_code: int, the_date: date) -> list[dict]:
    """One GetBookingList call: all bookings for a site/date, most-recent status."""
    base = settings.ft_base.rstrip("/")
    url = f"{base}/BookingApi/Booking/GetBookingList/{token}"
    params = {"SiteCode": site_code, "ShiftCode": 0, "BookingDate": the_date.strftime("%Y%m%d")}
    for attempt in range(5):
        resp = session.get(url, params=params, timeout=settings.http_timeout,
                           headers={"Accept": "application/json"})
        if resp.status_code in (429, 500, 502, 503, 504):
            wait = min(2 ** attempt, 30)
            ra = resp.headers.get("Retry-After")
            if ra and ra.isdigit():
                wait = int(ra)
            log.warning("HTTP %s site=%s date=%s; retry in %ss",
                        resp.status_code, site_code, the_date, wait)
            time.sleep(wait)
            continue
        if resp.status_code >= 400:
            log.error("HTTP %s site=%s date=%s body=%s",
                      resp.status_code, site_code, the_date, resp.text[:300])
        resp.raise_for_status()
        data = resp.json()
        if not data.get("IsSuccess", True):
            log.warning("FT API not-success site=%s date=%s err=%s desc=%s",
                        site_code, the_date, data.get("ErrorCode"), data.get("Description"))
        return data.get("ResultInfo") or []
    return []


# --- row mapping ------------------------------------------------------------
def _row(site_code: int, the_date: date, b: dict, blid) -> tuple | None:
    ref = _first(b, "BookingRefNo", "BookingRef")
    if ref in (None, ""):
        return None  # need the upsert key
    status_code = _int(_first(b, "BookingStatusCode", "StatusCode"))
    channel_code = _int(_first(b, "SaleChannelCode"))
    iface_code = _int(_first(b, "InterfaceTypeCode"))
    time_secs = _int(_first(b, "BookingTime", "BookingStartTime"))
    return (
        site_code, str(ref), _first(b, "BookingCode"), blid, SITE_NAMES.get(site_code),
        the_date, time_secs, _secs_to_time(time_secs), _duration_mins(b),
        _int(_first(b, "GuestCount", "Covers")),
        status_code, STATUS.get(status_code),
        channel_code, CHANNEL.get(channel_code),
        iface_code, INTERFACE.get(iface_code),
        str(_first(b, "TableNo", "TableList") or "") or None,
        _bool(_first(b, "IsRewardMember")), _int(_first(b, "Visits")),
        _num(_first(b, "TotalAmount")), _num(_first(b, "Deposit")),
        _first(b, "FirstName"), _first(b, "LastName"),
        _first(b, "Email"), _first(b, "Tel", "Mobile", "Telephone"),
        _bool(_first(b, "SpecialOfferEmail", "OptInEmail")),
        _bool(_first(b, "SpecialOfferMobile", "OptInMobile")),
        _first(b, "CreatedOn"),   # when the booking was made -> pre-book vs same-day
        psycopg2.extras.Json(b),
    )


_COLS = [
    "ft_site_code", "booking_ref_no", "booking_code", "business_location_id", "site_name",
    "booking_date", "booking_time_secs", "booking_time", "duration_mins", "guest_count",
    "status_code", "status", "sale_channel_code", "sale_channel",
    "interface_type_code", "interface_type", "table_no", "is_reward_member", "visits",
    "total_amount", "deposit", "first_name", "last_name", "email", "tel",
    "opt_in_email", "opt_in_mobile", "created_on", "raw",
]

_UPSERT = f"""
INSERT INTO bookings ({','.join(_COLS)}, updated_at)
VALUES ({','.join(['%s'] * len(_COLS))}, now())
ON CONFLICT (ft_site_code, booking_ref_no) DO UPDATE SET
  {', '.join(f'{c}=EXCLUDED.{c}' for c in _COLS if c not in ('ft_site_code', 'booking_ref_no'))},
  updated_at=now();
"""


def _load_blid_map(conn) -> dict[int, int]:
    """ft_site_code -> business_location_id from ft_site_map (filled by migrate)."""
    with conn.cursor() as cur:
        cur.execute("SELECT ft_site_code, business_location_id FROM ft_site_map "
                    "WHERE business_location_id IS NOT NULL")
        return {int(c): int(b) for c, b in cur.fetchall()}


# --- date helpers -----------------------------------------------------------
def _date_range(start: date, end: date) -> list[date]:
    n = (end - start).days
    return [start + timedelta(days=i) for i in range(n + 1)]


def _resolve_dates(args) -> list[date]:
    today = date.today()
    if args.date:
        d = datetime.strptime(args.date, "%Y-%m-%d").date()
        return [d]
    if args.from_date or args.to_date:
        start = datetime.strptime(args.from_date, "%Y-%m-%d").date() if args.from_date \
            else today - timedelta(days=settings.ft_window_days)
        end = datetime.strptime(args.to_date, "%Y-%m-%d").date() if args.to_date else today
        return _date_range(start, end)
    days = args.days if args.days is not None else settings.ft_window_days
    return _date_range(today - timedelta(days=days), today)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Favourite Table bookings ingestion (pull API -> Neon)")
    p.add_argument("--date", help="single dine date YYYY-MM-DD")
    p.add_argument("--from", dest="from_date", help="range start YYYY-MM-DD (inclusive)")
    p.add_argument("--to", dest="to_date", help="range end YYYY-MM-DD (inclusive)")
    p.add_argument("--days", type=int, help="pull the last N days ending today")
    p.add_argument("--site", type=int, help="restrict to one FT SiteCode")
    p.add_argument("--dry-run", action="store_true", help="fetch + parse, log a sample, no DB write")
    args = p.parse_args(argv)

    # Skip cleanly (no failure) until the token is configured -- same guard
    # nory.py uses for AWS creds -- so the nightly job ships dark.
    token = settings.ft_auth_token
    if not token:
        log.warning("Favourite Table: FT_AUTH_TOKEN not set -- skipping bookings ingest.")
        return 0

    site_codes = [args.site] if args.site else list(settings.ft_site_codes)
    dates = _resolve_dates(args)
    log.info("Pulling %d site(s) x %d date(s) = %d calls (%s..%s)",
             len(site_codes), len(dates), len(site_codes) * len(dates), dates[0], dates[-1])

    session = requests.Session()
    conn = None if args.dry_run else psycopg2.connect(settings.database_url)
    blid_map = _load_blid_map(conn) if conn else {}
    total = 0
    try:
        for sc in site_codes:
            blid = blid_map.get(sc)
            site_total = 0
            for d in dates:
                bookings = _get_bookings(session, token, sc, d)
                rows = [r for r in (_row(sc, d, b, blid) for b in bookings) if r]
                if rows and conn:
                    with conn.cursor() as cur:
                        psycopg2.extras.execute_batch(cur, _UPSERT, rows, page_size=200)
                    conn.commit()
                site_total += len(rows)
                if args.dry_run and rows:
                    s = rows[0]
                    log.info("   %s %s: %d bookings (e.g. ref=%s status=%s covers=%s time=%s)",
                             SITE_NAMES.get(sc), d, len(rows), s[1], s[11], s[9], s[7])
                if settings.ft_throttle_secs:
                    time.sleep(settings.ft_throttle_secs)
            log.info("[%s/%s] %d booking(s) upserted%s",
                     sc, SITE_NAMES.get(sc), site_total, " (dry-run)" if args.dry_run else "")
            total += site_total
        log.info("Bookings ingest complete: %d booking(s) across %d site(s)", total, len(site_codes))
    finally:
        if conn:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
