"""Guest data-quality check for one site's win-back list (Nova, 18 Nov 2026).

Read-only. Takes the Favourite Table guest CSV (raw details) plus the bookings in
Neon, finds the site's "regulars gone quiet" (same rule as guest_activity: top site
= the site, 2+ visits, last visit over 90 days ago, nothing booked) and reports how
clean their contact details are, and which look like near-duplicates of other guests.

Privacy: prints COUNTS and a few MASKED examples only (e.g. jo***@gmail.com, J.).
Nothing is written to any file or database. Run on your own machine via Railway:
    railway run -s como-webhook python -m ingest.guest_quality --csv "<GuestList.csv>" --site Solihull
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from datetime import timedelta

from .config import settings
from .como import _hash, _norm_email, _norm_phone
from .guest_activity import SQL_BOOKINGS, aggregate, build_maps, uk_today

POPULAR = ["gmail.com", "googlemail.com", "hotmail.com", "hotmail.co.uk", "yahoo.com", "yahoo.co.uk",
           "outlook.com", "outlook.co.uk", "icloud.com", "live.com", "live.co.uk", "btinternet.com",
           "sky.com", "aol.com", "msn.com", "me.com", "talktalk.net", "virginmedia.com", "ntlworld.com",
           "blueyonder.co.uk", "protonmail.com", "mail.com"]
BAD_LOCALS = {"info", "admin", "test", "noreply", "no-reply", "none", "na", "example", "unknown", "guest", "customer"}
BAD_DOMAINS = {"example.com", "test.com", "email.com", "none.com", "noemail.com"}
BAD_NAMES = {"test", "guest", "unknown", "n/a", "na", "customer", "none", "tbc", "walkin", "walk-in", "walk in"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$")


def lev(a, b, maxd=2):
    if abs(len(a) - len(b)) > maxd:
        return maxd + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > maxd:
            return maxd + 1
        prev = cur
    return prev[-1]


def mask_email(e):
    l, _, d = (e or "").partition("@")
    return f"{l[:2]}***@{d}"


def mask_name(n):
    n = (n or "").strip()
    return f"{n[:1]}." if n else "(blank)"


def clean_phone(mobile, tel):
    """Return (kind, digits10). kind in valid_mobile, landline, invalid, placeholder, none, mobile_in_tel."""
    def norm(s):
        d = re.sub(r"\D", "", s or "")
        if d.startswith("0044"):
            d = "0" + d[4:]
        elif d.startswith("44") and len(d) == 12:
            d = "0" + d[2:]
        elif len(d) == 10 and d[0] in "123789" and not d.startswith("0"):
            d = "0" + d
        return d
    m, t = norm(mobile), norm(tel)
    d = m or t
    if not d:
        return "none", ""
    if len(set(d)) <= 2 or d in ("07000000000", "01234567890", "07123456789"):
        return "placeholder", d[-10:]
    if len(d) == 11 and d.startswith("07"):
        return ("valid_mobile" if m else "mobile_in_tel_field"), d[-10:]
    if len(d) in (10, 11) and d[:2] in ("01", "02", "03"):
        return "landline", d[-10:]
    return "invalid", d[-10:]


def read_raw(path):
    ids = {}
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        for r in csv.DictReader(f):
            email = (r.get("Email") or "").strip().lower()
            kind, ph10 = clean_phone(r.get("Mobile"), r.get("Tel"))
            eh = _hash(_norm_email(email))
            phash = _hash(_norm_phone(r.get("Mobile")) or _norm_phone(r.get("Tel")))
            key = ("e", eh) if eh else ("p", phash)
            if key == ("p", None):
                continue
            rec = {"first": (r.get("FirstName") or "").strip(), "last": (r.get("LastName") or "").strip(),
                   "email": email, "phone_kind": kind, "ph10": ph10,
                   "postcode": ((r.get("Postcode") or "").strip().upper().split() or [""])[0],
                   "email_consent": r.get("EmailOptIn"), "sms_consent": r.get("SMSOptin"),
                   "eh": eh, "phash": phash}
            if key in ids:
                old = ids[key]
                for k in ("first", "last", "postcode"):
                    old[k] = old[k] or rec[k]
                if old["phone_kind"] in ("none", "invalid", "placeholder") and rec["phone_kind"] not in ("none",):
                    old["phone_kind"], old["ph10"] = rec["phone_kind"], rec["ph10"]
            else:
                ids[key] = rec
    return ids


def email_issues(rec):
    e, out = rec["email"], []
    if not e:
        return ["no_email"]
    if not EMAIL_RE.match(e):
        out.append("invalid_syntax")
        return out
    local, _, dom = e.partition("@")
    if "privaterelay.appleid.com" in dom:
        out.append("apple_private_relay")
    if local in BAD_LOCALS or dom in BAD_DOMAINS:
        out.append("role_or_placeholder")
    if dom not in POPULAR:
        near = [p for p in POPULAR if 0 < lev(dom, p, 2) <= 2]
        if near:
            out.append("domain_typo")
    return out


def name_issues(rec):
    f, l, out = rec["first"], rec["last"], []
    if not f:
        return ["first_name_missing"]
    if len(f.strip(".")) == 1:
        out.append("first_name_initial_only")
    if re.search(r"\d|@", f):
        out.append("first_name_has_digits_or_at")
    if f.lower() in BAD_NAMES:
        out.append("placeholder_name")
    if l and f.lower() == l.lower():
        out.append("first_equals_last")
    if len(f) > 3 and (f.islower() or f.isupper()):
        out.append("odd_capitalisation")
    if not l:
        out.append("last_name_missing")
    local = rec["email"].partition("@")[0]
    if rec["email"] and f.lower() == local.lower():
        out.append("name_is_email_local")
    return out


def near_misses(recs, subset_keys):
    """subset_keys: keys of the regulars. Compare them against ALL guests."""
    by_phone, by_name, by_block, by_local = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    for k, r in recs.items():
        if r["ph10"] and r["phone_kind"] in ("valid_mobile", "mobile_in_tel_field", "landline"):
            by_phone[r["ph10"]].append(k)
        if r["first"] and r["last"] and r["postcode"]:
            by_name[(r["first"].lower(), r["last"].lower(), r["postcode"])].append(k)
        if r["email"] and "@" in r["email"]:
            l, _, d = r["email"].partition("@")
            if len(l) >= 4:
                by_block[(d, l[:3])].append(k)
                by_block[(d, "~" + l[-3:])].append(k)
            if len(l) >= 6:
                by_local[l].append(k)
    found = {"same_phone_other_email": {}, "same_name_postcode_other_email": {}, "email_typo_variant": {},
             "same_local_other_domain": {}}
    for k in subset_keys:
        r = recs[k]
        e = r["email"]
        l, _, d = e.partition("@")
        for o in by_phone.get(r["ph10"], []):
            if o != k and recs[o]["email"] != e:
                found["same_phone_other_email"].setdefault(k, o)
        nm = (r["first"].lower(), r["last"].lower(), r["postcode"])
        if all(nm):
            for o in by_name.get(nm, []):
                if o != k and recs[o]["email"] != e:
                    found["same_name_postcode_other_email"].setdefault(k, o)
        if len(l) >= 4:
            for blk in ((d, l[:3]), (d, "~" + l[-3:])):
                for o in by_block.get(blk, []):
                    if o == k:
                        continue
                    ol = recs[o]["email"].partition("@")[0]
                    if ol != l and lev(l, ol, 2) <= 2:
                        found["email_typo_variant"].setdefault(k, o)
        if len(l) >= 6:
            for o in by_local.get(l, []):
                if o != k and recs[o]["email"].partition("@")[2] != d:
                    found["same_local_other_domain"].setdefault(k, o)
    return found


def analyse(recs, regular_keys):
    rep = {"regulars": len(regular_keys)}
    ec, nc, pc = Counter(), Counter(), Counter()
    clean = 0
    for k in regular_keys:
        r = recs[k]
        ei, ni = email_issues(r), name_issues(r)
        ec.update(ei); nc.update(ni); pc[r["phone_kind"]] += 1
        if r["sms_consent"] == "OptedIn" and r["phone_kind"] not in ("valid_mobile", "mobile_in_tel_field"):
            pc["sms_opted_in_but_no_valid_mobile"] += 1
        if not ei and not ni and r["phone_kind"] == "valid_mobile":
            clean += 1
    rep["fully_clean_(email ok, name ok, valid mobile)"] = clean
    rep["email_issues"] = dict(ec)
    rep["name_issues"] = dict(nc)
    rep["phone"] = dict(pc)
    nm = near_misses(recs, regular_keys)
    rep["near_miss_counts"] = {k: len(v) for k, v in nm.items()}
    rep["near_miss_masked_examples"] = {
        k: [f"{mask_email(recs[a]['email'])} ~ {mask_email(recs[b]['email'])}  ({mask_name(recs[a]['first'])} / {mask_name(recs[b]['first'])})"
            for a, b in list(v.items())[:5]] for k, v in nm.items()}
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--site", default="Solihull")
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
    reg = [key_of[s["guest_id"]] for s in stats
           if s["top_site"] == args.site and s["visits"] >= 2 and s["last_visit"] and s["last_visit"] < cut and s["upcoming"] == 0]
    print(f"as of {today} (UK). Site: {args.site}. Guests in file: {len(recs)}")
    rep = analyse(recs, reg)
    for k, v in rep.items():
        if isinstance(v, dict) and k == "near_miss_masked_examples":
            print(f"{k}:")
            for kk, vv in v.items():
                print(f"  {kk}:")
                for line in vv:
                    print(f"    {line}")
        else:
            print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
