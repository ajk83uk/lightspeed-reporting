"""Guest activity builder (Nova, 18 Nov 2026).

Links Favourite Table bookings to public.guest and writes one summary row per
guest into public.guest_activity: visits, no-shows, cancellations, first and last
visit, next booking, last and top site, average party size.

Privacy: bookings hold raw email/phone, so the matching is done here in Python.
Emails and phones are hashed with the SAME salt and normalisation as the Como
ingest and the guest loader, then matched to public.guest hashes. Nothing raw is
written anywhere. public.guest_activity has no grants for dashboard_ro.

Definitions (state them whenever a figure is quoted)
  * visit        = booking status Show or Complete, dated on or before today (UK).
  * no-show      = status NoShow.   cancellation = status Cancelled.
  * upcoming     = live (not cancelled) booking dated after today.
  * past_unrecorded = past booking never marked Show/NoShow/Cancelled/Complete.
  * History starts when the booking pull began (Dec 2024). Walk-ins with no contact
    details cannot be matched, so visits are a FLOOR, not the full count.
  * Matching: email hash first; else phone hash, but only when that phone belongs
    to exactly one guest (shared phones are skipped).

Usage (dry-run is the default; it only READS the database):
    python -m ingest.guest_activity
    python -m ingest.guest_activity --commit
Run through Railway so the salt and DATABASE_URL are supplied:
    railway run -s como-webhook python -m ingest.guest_activity
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from .config import settings
from .como import _hash, _norm_email, _norm_phone

DDL = """
create table if not exists public.guest_activity (
  guest_id        bigint primary key references public.guest(guest_id) on delete cascade,
  bookings_total  int not null default 0,
  visits          int not null default 0,
  noshows         int not null default 0,
  cancellations   int not null default 0,
  past_unrecorded int not null default 0,
  upcoming        int not null default 0,
  first_visit     date,
  last_visit      date,
  next_booking    date,
  last_site       text,
  top_site        text,
  avg_party       numeric(5,1),
  updated_at      timestamptz not null default now()
);
revoke all on public.guest_activity from public;
revoke all on public.guest_activity from dashboard_ro;
"""

SHOWED = {"Show", "Complete"}
SQL_BOOKINGS = ("select email, raw->>'Tel', booking_date, status, guest_count, site_name "
                "from public.bookings where booking_date is not null")
SQL_GUESTS = "select guest_id, email_hash, phone_hash from public.guest"
INSERT = ("insert into public.guest_activity (guest_id, bookings_total, visits, noshows, cancellations, "
          "past_unrecorded, upcoming, first_visit, last_visit, next_booking, last_site, top_site, avg_party) "
          "values (%(guest_id)s, %(bookings_total)s, %(visits)s, %(noshows)s, %(cancellations)s, "
          "%(past_unrecorded)s, %(upcoming)s, %(first_visit)s, %(last_visit)s, %(next_booking)s, "
          "%(last_site)s, %(top_site)s, %(avg_party)s)")


def uk_today() -> date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/London")).date()
    except Exception:
        return datetime.now(timezone.utc).date()


def build_maps(guest_rows):
    """Return (email_hash -> guest_id, phone_hash -> guest_id for unambiguous phones only)."""
    em, ph_all = {}, defaultdict(set)
    for gid, eh, ph in guest_rows:
        if eh:
            em[eh] = gid
        if ph:
            ph_all[ph].add(gid)
    return em, {p: next(iter(g)) for p, g in ph_all.items() if len(g) == 1}


def aggregate(bookings, em, ph, today):
    """bookings: iterable of (email, tel, booking_date, status, guests, site). Returns (stats, counters)."""
    acc = defaultdict(lambda: {"bookings_total": 0, "visits": 0, "noshows": 0, "cancellations": 0,
                               "past_unrecorded": 0, "upcoming": 0, "first_visit": None, "last_visit": None,
                               "next_booking": None, "sites": Counter(), "last_site": None,
                               "party_sum": 0, "party_n": 0})
    c = Counter()
    for email, tel, bdate, status, guests, site in bookings:
        c["bookings_read"] += 1
        eh = _hash(_norm_email(email))
        pho = _hash(_norm_phone(tel))
        gid, how = None, None
        if eh and eh in em:
            gid, how = em[eh], "email"
        elif pho and pho in ph:
            gid, how = ph[pho], "phone"
        if gid is None:
            c["unmatched"] += 1
            continue
        c["matched_" + how] += 1
        a = acc[gid]
        a["bookings_total"] += 1
        if status == "Cancelled":
            a["cancellations"] += 1
            continue
        if bdate > today:
            a["upcoming"] += 1
            if a["next_booking"] is None or bdate < a["next_booking"]:
                a["next_booking"] = bdate
            continue
        if status in SHOWED:
            a["visits"] += 1
            a["sites"][site] += 1
            if a["first_visit"] is None or bdate < a["first_visit"]:
                a["first_visit"] = bdate
            if a["last_visit"] is None or bdate > a["last_visit"]:
                a["last_visit"], a["last_site"] = bdate, site
            if guests:
                a["party_sum"] += guests
                a["party_n"] += 1
        elif status == "NoShow":
            a["noshows"] += 1
        else:
            a["past_unrecorded"] += 1
    out = []
    for gid, a in acc.items():
        out.append({"guest_id": gid, "bookings_total": a["bookings_total"], "visits": a["visits"],
                    "noshows": a["noshows"], "cancellations": a["cancellations"],
                    "past_unrecorded": a["past_unrecorded"], "upcoming": a["upcoming"],
                    "first_visit": a["first_visit"], "last_visit": a["last_visit"],
                    "next_booking": a["next_booking"], "last_site": a["last_site"],
                    "top_site": a["sites"].most_common(1)[0][0] if a["sites"] else None,
                    "avg_party": round(a["party_sum"] / a["party_n"], 1) if a["party_n"] else None})
    return out, c


def summarise(rows, c, n_guests, today):
    lapsed_cut = today - timedelta(days=90)
    return {
        "bookings_read": c["bookings_read"], "matched_by_email": c["matched_email"],
        "matched_by_phone": c["matched_phone"], "unmatched_bookings": c["unmatched"],
        "guests_total": n_guests, "guests_with_any_booking": len(rows),
        "guests_with_1_visit": sum(1 for r in rows if r["visits"] == 1),
        "guests_with_2plus_visits": sum(1 for r in rows if r["visits"] >= 2),
        "guests_with_upcoming_booking": sum(1 for r in rows if r["upcoming"] > 0),
        "regulars_gone_quiet_90d (2+ visits, last visit >90d ago, nothing booked)":
            sum(1 for r in rows if r["visits"] >= 2 and r["last_visit"] and r["last_visit"] < lapsed_cut and r["upcoming"] == 0),
        "noshow_guests": sum(1 for r in rows if r["noshows"] > 0),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true", help="write guest_activity (default is dry-run)")
    args = ap.parse_args(argv)
    if not settings.como_hash_salt:
        print("COMO_HASH_SALT is not set - refusing to run.", file=sys.stderr)
        return 2
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(settings.database_url)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(SQL_GUESTS)
            guests = cur.fetchall()
            cur.execute(SQL_BOOKINGS)
            bookings = cur.fetchall()
        em, ph = build_maps(guests)
        today = uk_today()
        rows, c = aggregate(bookings, em, ph, today)
        print(f"as of {today} (UK)")
        for k, v in summarise(rows, c, len(guests), today).items():
            print(f"{k}: {v}")
        if not args.commit:
            print("DRY RUN - nothing written. Add --commit to write public.guest_activity.")
            return 0
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute("delete from public.guest_activity")
            psycopg2.extras.execute_batch(cur, INSERT, rows, page_size=500)
            cur.execute("select count(*) from public.guest_activity")
            n = cur.fetchone()[0]
        print(f"LOADED. public.guest_activity now has {n} guests.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
