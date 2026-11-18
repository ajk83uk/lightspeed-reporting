"""Guest record loader (Nova, 18 Nov 2026).

Loads the Favourite Table "Download Guest Data" CSV into public.guest.

Privacy by design
  * Guests are keyed on SALTED HASHES of email and phone, using exactly the same
    salt and normalisation as the Como ingest (ingest/como.py), so a guest here
    matches the same person in como_members.
  * Raw email addresses and phone numbers are NEVER stored by this loader.
  * Kept for personalising: first name, postcode district (outward code only),
    occasion (birthday), per-channel consent, FT created date.
  * public.guest has no grants for dashboard_ro or any app role.

Consent is per channel (email, sms). When the same person appears more than once,
the most restrictive value wins: OptedOut < NoConsent < OptedIn.

Usage (dry-run is the default and never touches the database):
    python -m ingest.guests --csv "C:\\path\\GuestList.csv"
    python -m ingest.guests --csv "C:\\path\\GuestList.csv" --commit
Needs COMO_HASH_SALT in the environment (and DATABASE_URL for --commit).
"""
from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from collections import Counter
from datetime import datetime

from .config import settings
from .como import _hash, _norm_email, _norm_phone

log = logging.getLogger("guests")

RANK = {"OptedOut": 0, "NoConsent": 1, "OptedIn": 2}

DDL = """
create table if not exists public.guest (
  guest_id          bigserial primary key,
  email_hash        text,
  phone_hash        text,
  first_name        text,
  postcode_district text,
  occasion          text,
  email_consent     text,
  sms_consent       text,
  ft_created_on     date,
  in_ft             boolean not null default false,
  in_como           boolean not null default false,
  updated_at        timestamptz not null default now()
);
create unique index if not exists guest_email_hash_uq on public.guest (email_hash) where email_hash is not null;
create unique index if not exists guest_phone_only_uq on public.guest (phone_hash) where email_hash is null and phone_hash is not null;
create index if not exists guest_phone_hash_ix on public.guest (phone_hash);
revoke all on public.guest from public;
revoke all on public.guest from dashboard_ro;
"""

_COLS = ("email_hash, phone_hash, first_name, postcode_district, occasion, "
         "email_consent, sms_consent, ft_created_on, in_ft")
_SET = ("phone_hash = coalesce(excluded.phone_hash, public.guest.phone_hash), "
        "first_name = coalesce(excluded.first_name, public.guest.first_name), "
        "postcode_district = coalesce(excluded.postcode_district, public.guest.postcode_district), "
        "occasion = coalesce(excluded.occasion, public.guest.occasion), "
        "email_consent = excluded.email_consent, sms_consent = excluded.sms_consent, "
        "ft_created_on = coalesce(excluded.ft_created_on, public.guest.ft_created_on), "
        "in_ft = true, updated_at = now()")
UPSERT_EMAIL = (f"insert into public.guest ({_COLS}) values (%(email_hash)s, %(phone_hash)s, %(first_name)s, "
                "%(postcode_district)s, %(occasion)s, %(email_consent)s, %(sms_consent)s, %(ft_created_on)s, true) "
                f"on conflict (email_hash) where email_hash is not null do update set {_SET}")
UPSERT_PHONE = (f"insert into public.guest ({_COLS}) values (null, %(phone_hash)s, %(first_name)s, "
                "%(postcode_district)s, %(occasion)s, %(email_consent)s, %(sms_consent)s, %(ft_created_on)s, true) "
                f"on conflict (phone_hash) where email_hash is null and phone_hash is not null do update set {_SET}")
LINK_COMO = ("update public.guest g set in_como = true from public.como_members m "
             "where (m.email_hash is not null and m.email_hash = g.email_hash) "
             "or (m.phone_hash is not null and m.phone_hash = g.phone_hash)")


def _clean(v):
    s = (v or "").strip()
    return s or None


def _name(v):
    s = _clean(v)
    return s.title() if s else None


def _district(v):
    s = _clean(v)
    return s.upper().split()[0] if s else None


def _date(v):
    s = _clean(v)
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt).date()
        except ValueError:
            continue
    return None


def _worst(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a if RANK.get(a, 1) <= RANK.get(b, 1) else b


def read_guests(path):
    """Return (identities, stats). identities: key -> record dict."""
    ids, stats = {}, Counter()
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        for r in csv.DictReader(f):
            stats["rows"] += 1
            eh = _hash(_norm_email(r.get("Email")))
            ph = _hash(_norm_phone(r.get("Mobile")) or _norm_phone(r.get("Tel")))
            if not eh and not ph:
                stats["skipped_no_contact"] += 1
                continue
            key = ("e", eh) if eh else ("p", ph)
            rec = {"email_hash": eh, "phone_hash": ph, "first_name": _name(r.get("FirstName")),
                   "postcode_district": _district(r.get("Postcode")), "occasion": _clean(r.get("Occasion")),
                   "email_consent": _clean(r.get("EmailOptIn")), "sms_consent": _clean(r.get("SMSOptin")),
                   "ft_created_on": _date(r.get("CreatedOn"))}
            if key in ids:
                stats["merged_duplicates"] += 1
                old = ids[key]
                for k in ("phone_hash", "first_name", "postcode_district", "occasion"):
                    old[k] = old[k] or rec[k]
                old["email_consent"] = _worst(old["email_consent"], rec["email_consent"])
                old["sms_consent"] = _worst(old["sms_consent"], rec["sms_consent"])
                if rec["ft_created_on"] and (not old["ft_created_on"] or rec["ft_created_on"] < old["ft_created_on"]):
                    old["ft_created_on"] = rec["ft_created_on"]
            else:
                ids[key] = rec
    return ids, stats


def summarise(ids, stats):
    recs = list(ids.values())
    email_keyed = [r for r in recs if r["email_hash"]]
    phones = Counter(r["phone_hash"] for r in recs if r["phone_hash"])
    out = {
        "csv_rows": stats["rows"], "skipped_no_contact": stats["skipped_no_contact"],
        "merged_duplicates": stats["merged_duplicates"], "unique_guests": len(recs),
        "email_keyed": len(email_keyed), "phone_only": len(recs) - len(email_keyed),
        "phones_shared_by_2plus_guests": sum(1 for c in phones.values() if c > 1),
        "email_consent": dict(Counter(r["email_consent"] for r in recs)),
        "sms_consent": dict(Counter(r["sms_consent"] for r in recs)),
        "with_first_name": sum(1 for r in recs if r["first_name"]),
        "with_phone_hash": sum(1 for r in recs if r["phone_hash"]),
        "with_birthday": sum(1 for r in recs if r["occasion"]),
    }
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--commit", action="store_true", help="write to Neon (default is dry-run)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not settings.como_hash_salt:
        print("COMO_HASH_SALT is not set - refusing to run (would store no hashes).", file=sys.stderr)
        return 2
    ids, stats = read_guests(args.csv)
    for k, v in summarise(ids, stats).items():
        print(f"{k}: {v}")
    if not args.commit:
        print("DRY RUN - nothing written. Add --commit to load into Neon.")
        return 0
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(settings.database_url)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
            e = [r for r in ids.values() if r["email_hash"]]
            p = [r for r in ids.values() if not r["email_hash"]]
            psycopg2.extras.execute_batch(cur, UPSERT_EMAIL, e, page_size=500)
            psycopg2.extras.execute_batch(cur, UPSERT_PHONE, p, page_size=500)
            cur.execute(LINK_COMO)
            cur.execute("select count(*), count(*) filter (where in_como) from public.guest")
            total, como = cur.fetchone()
        print(f"LOADED. public.guest now has {total} guests; {como} also Como members.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
