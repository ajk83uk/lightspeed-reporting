"""Export one site's 'regulars gone quiet' to a CSV on THIS computer for the owner to eyeball.

Contains real names, emails and phones, so it is written to a path you choose (default: your Downloads
folder, outside the repo) and is never printed to the console or uploaded anywhere. Read-only: nothing is
written to the database.

    railway run -s como-webhook python -m ingest.guest_list --csv "<FT guest export>" --site Solihull
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import timedelta

from .config import settings
from .guest_activity import SQL_BOOKINGS, aggregate, build_maps, uk_today
from .guest_quality import (clean_phone, email_issues, name_issues, near_misses, read_raw)

LABEL = {"same_phone_other_email": "same phone, different email",
         "same_name_postcode_other_email": "same name + postcode, different email",
         "email_typo_variant": "email 1-2 letters different",
         "same_local_other_domain": "same email name, different provider"}


def fmt_phone(ph10):
    if not ph10 or len(ph10) != 10:
        return ""
    d = "0" + ph10
    return f"{d[:5]} {d[5:]}"          # the space stops Excel dropping the leading zero


def full_name(r):
    return " ".join(x for x in (r["first"], r["last"]) if x)


def build_rows(recs, stats_by_key, regular_keys):
    nm = near_misses(recs, regular_keys)
    rows = []
    for k in regular_keys:
        r, s = recs[k], stats_by_key[k]
        issues = email_issues(r) + name_issues(r)
        if r["phone_kind"] not in ("valid_mobile", "mobile_in_tel_field"):
            issues.append("phone_" + r["phone_kind"])
        dup_kind, dup_name, dup_email = "", "", ""
        for kind in LABEL:
            o = nm[kind].get(k)
            if o is not None:
                dup_kind, dup_name, dup_email = LABEL[kind], full_name(recs[o]), recs[o]["email"]
                break
        rows.append({
            "name": full_name(r), "email": r["email"], "mobile": fmt_phone(r["ph10"]),
            "visits": s["visits"], "first_visit": s["first_visit"], "last_visit": s["last_visit"],
            "last_site": s["last_site"], "avg_party": s["avg_party"], "noshows": s["noshows"],
            "email_consent": r["email_consent"], "sms_consent": r["sms_consent"],
            "data_issues": "; ".join(issues), "possible_duplicate_reason": dup_kind,
            "possible_duplicate_name": dup_name, "possible_duplicate_email": dup_email})
    rows.sort(key=lambda x: (-x["visits"], str(x["last_visit"])))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--site", default="Solihull")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    if not settings.como_hash_salt:
        print("COMO_HASH_SALT is not set - refusing to run.", file=sys.stderr)
        return 2
    recs = read_raw(args.csv)
    keys = list(recs)
    gid_of = {k: i + 1 for i, k in enumerate(keys)}
    key_of = {v: k for k, v in gid_of.items()}
    em, ph = build_maps([(gid_of[k], recs[k]["eh"], recs[k]["phash"]) for k in keys])
    import psycopg2
    conn = psycopg2.connect(settings.database_url)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(SQL_BOOKINGS)
            bookings = cur.fetchall()
    finally:
        conn.close()
    today = uk_today()
    stats, _ = aggregate(bookings, em, ph, today)
    cut = today - timedelta(days=90)
    by_key = {key_of[s["guest_id"]]: s for s in stats}
    reg = [k for k, s in by_key.items() if s["top_site"] == args.site and s["visits"] >= 2
           and s["last_visit"] and s["last_visit"] < cut and s["upcoming"] == 0]
    rows = build_rows(recs, by_key, reg)
    out = args.out or os.path.join(os.path.expanduser("~"), "Downloads", f"{args.site.lower()}-regulars-{today}.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["name"])
        w.writeheader()
        w.writerows(rows)
    flagged = sum(1 for r in rows if r["possible_duplicate_reason"])
    print(f"as of {today} (UK). {len(rows)} {args.site} regulars written to: {out}")
    print(f"{flagged} are flagged as possible duplicates. This file holds personal data - keep it off shared drives.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
