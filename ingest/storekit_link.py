"""Link StoreKit online-order customers to our guest/person tables and to Como members.

Default is a DRY RUN: writes nothing and prints counts only (no names, emails or phone numbers).
Input is the Customers export from StoreKit (Customers > Export).

    railway run -s como-webhook python -m ingest.storekit_link --csv "<path to customers CSV>" [--commit]

--commit replaces public.storekit_person (one row per StoreKit person: matched FT person, Como flag and source,
order count, last order date, spend, marketing opt-out; hashes only, no grants to dashboard_ro) and, for anyone
who opted out of marketing in StoreKit and matches an FT person, sets that person's email and SMS consent to
OptedOut in public.person. Re-run after guest_merge --commit, because that rebuilds public.person.

Rules
  * Email gets the same domain-typo repair as the guest merge; phone = last 10 digits, UK mobile only (07...).
  * Rows sharing an email or a valid mobile are one StoreKit person.
  * Test/staff rows are excluded: como.com, storekit.com, tapandtandoor.co.uk addresses and negative spend.
  * StoreKit marketing 'no' on any row = opted out for that person (stricter rule).
  * Matching to Neon is by hash (guest.email_hash / guest.phone_hash, como_members hashes).
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime
from collections import Counter, defaultdict

from .config import settings
from .como import _hash, _norm_email, _norm_phone
from .guest_merge import DSU, fix_domain

EXCLUDE_DOMAINS = ("como.com", "storekit.com", "tapandtandoor.co.uk")


def _money(s):
    m = re.sub(r"[^0-9.\-]", "", s or "")
    return float(m) if m not in ("", "-", ".") else 0.0


def _date(v):
    try:
        return datetime.strptime((v or "").strip(), "%d %b %Y").date()
    except ValueError:
        return None


def load_rows(path):
    out, excluded = [], Counter()
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            email = fix_domain(r.get("Email") or "")
            email = _norm_email(email)
            if email and email.split("@")[1].endswith(EXCLUDE_DOMAINS):
                excluded["test_or_staff_domain"] += 1
                continue
            if _money(r.get("Total spent")) < 0:
                excluded["negative_spend"] += 1
                continue
            ph = _norm_phone(r.get("Phone"))
            valid_mobile = bool(ph and ph.startswith("7"))
            out.append({
                "eh": _hash(email), "ph": _hash(ph) if valid_mobile else None,
                "orders": int(r.get("Order count") or 0),
                "spend_pence": int(round(_money(r.get("Total spent")) * 100)),
                "last": _date(r.get("Last order date")),
                "marketing": (r.get("Marketing") or "").strip().lower() == "yes",
                "bad_phone": not valid_mobile,
            })
    return out, excluded


def cluster(rows):
    dsu = DSU(len(rows))
    seen = {}
    for i, r in enumerate(rows):
        for key in (("e", r["eh"]), ("p", r["ph"])):
            if key[1] is None:
                continue
            if key in seen:
                dsu.union(i, seen[key])
            else:
                seen[key] = i
    people = defaultdict(list)
    for i in range(len(rows)):
        people[dsu.find(i)].append(rows[i])
    return list(people.values())


def analyse(people, guests, guest_person, person_in_como, visits, como):
    """people: clusters of StoreKit rows. guests: (guest_id, email_hash, phone_hash). guest_person: {guest_id: person_id}.
    person_in_como: {person_id: bool}. visits: {person_id: int}. como: list of (email_hash, phone_hash, source)."""
    g_by_e = {g[1]: g[0] for g in guests if g[1]}
    g_by_p = defaultdict(set)
    for g in guests:
        if g[2]:
            g_by_p[g[2]].add(g[0])
    c_by_e, c_by_p = {}, {}
    for e, p, src in como:
        if e:
            c_by_e[e] = src
        if p:
            c_by_p[p] = src
    n, recs = Counter(), []
    for grp in people:
        ehs = {r["eh"] for r in grp if r["eh"]}
        phs = {r["ph"] for r in grp if r["ph"]}
        orders = sum(r["orders"] for r in grp)
        gids = {g_by_e[e] for e in ehs if e in g_by_e}
        for p in phs:
            gids |= g_by_p.get(p, set())
        pids = {guest_person[g] for g in gids if g in guest_person}
        src = {c_by_e[e] for e in ehs if e in c_by_e} | {c_by_p[p] for p in phs if p in c_by_p}
        n["storekit_people"] += 1
        n["storekit_orders"] += orders
        if orders > 1:
            n["repeat_orderers"] += 1
        if any(not r["marketing"] for r in grp):
            n["marketing_opt_out"] += 1
        if not phs:
            n["no_valid_mobile"] += 1
        in_ft = bool(pids)
        booked = any(visits.get(p, 0) > 0 for p in pids)
        in_como = bool(src)
        n["in_ft_guest_list"] += in_ft
        n["has_a_visit"] += booked
        n["in_como"] += in_como
        n["online_only_not_in_ft"] += (not in_ft)
        n["no_app_and_never_booked"] += (not in_como and not booked)
        n["in_como_and_booked_and_ordered"] += (in_como and booked)
        n["in_como_not_in_ft"] += (in_como and not in_ft)
        n["booked_not_in_como"] += (booked and not in_como)
        for s in src:
            n[f"como_source:{s}"] += 1
        if len(pids) > 1:
            n["matches_more_than_one_ft_person"] += 1
        lasts = [r["last"] for r in grp if r["last"]]
        recs.append({
            "person_id": min(pids) if pids else None, "ft_matches": len(pids),
            "email_hash": sorted(ehs)[0] if ehs else None, "phone_hash": sorted(phs)[0] if phs else None,
            "orders": orders, "spend_pence": sum(r["spend_pence"] for r in grp),
            "last_order_date": max(lasts) if lasts else None,
            "marketing_opt_out": any(not r["marketing"] for r in grp),
            "in_como": in_como,
            "como_source": ("App" if "App" in src else (sorted(src)[0] if src else None)),
            "has_visit": booked,
        })
    return n, recs


DDL = """
create table if not exists public.storekit_person (
  sk_person_id      serial primary key,
  person_id         bigint,
  ft_matches        int not null default 0,
  email_hash        text,
  phone_hash        text,
  orders            int not null default 0,
  spend_pence       bigint not null default 0,
  last_order_date   date,
  marketing_opt_out boolean not null default false,
  in_como           boolean not null default false,
  como_source       text,
  has_visit         boolean not null default false,
  updated_at        timestamptz not null default now()
);
create index if not exists storekit_person_person_ix on public.storekit_person (person_id);
revoke all on public.storekit_person from public;
revoke all on public.storekit_person from dashboard_ro;
"""
COLS = ("person_id", "ft_matches", "email_hash", "phone_hash", "orders", "spend_pence", "last_order_date",
        "marketing_opt_out", "in_como", "como_source", "has_visit")


def save(conn, recs):
    from psycopg2.extras import execute_values
    with conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute("delete from public.storekit_person")
        execute_values(cur, f"insert into public.storekit_person ({', '.join(COLS)}) values %s",
                       [tuple(r[c] for c in COLS) for r in recs])
        cur.execute("update public.person set email_consent = 'OptedOut', sms_consent = 'OptedOut', updated_at = now() "
                    "where person_id in (select person_id from public.storekit_person "
                    "where marketing_opt_out and person_id is not null)")
        suppressed = cur.rowcount
    conn.commit()
    return suppressed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--commit", action="store_true", help="save public.storekit_person (default is a dry run)")
    args = ap.parse_args(argv)
    if not settings.como_hash_salt:
        print("COMO_HASH_SALT is not set - run with: railway run -s como-webhook python -m ingest.storekit_link ...")
        return 2
    rows, excluded = load_rows(args.csv)
    people = cluster(rows)
    import psycopg2
    conn = psycopg2.connect(settings.database_url.strip())
    conn.set_session(readonly=True)  # the dry run can never write
    cur = conn.cursor()
    cur.execute("select guest_id, email_hash, phone_hash from public.guest")
    guests = cur.fetchall()
    cur.execute("select guest_id, person_id from public.guest_person")
    gp = dict(cur.fetchall())
    cur.execute("select person_id, in_como from public.person")
    pc = dict(cur.fetchall())
    cur.execute("select person_id, visits from public.person_activity")
    vis = dict(cur.fetchall())
    cur.execute("select email_hash, phone_hash, registration_source from public.como_members")
    como = cur.fetchall()
    conn.close()
    n, recs = analyse(people, guests, gp, pc, vis, como)
    print(f"rows read {len(rows)}  excluded {dict(excluded)}  merged into {len(people)} StoreKit people")
    for k, v in sorted(n.items()):
        print(f"  {k:36s}{v}")
    if not args.commit:
        print("DRY RUN - nothing written.")
        return 0
    if len(recs) < 100:
        print(f"Only {len(recs)} StoreKit people - that does not look like the full export. Nothing written.")
        return 3
    wconn = psycopg2.connect(settings.database_url.strip())
    suppressed = save(wconn, recs)
    wconn.close()
    print(f"SAVED public.storekit_person: {len(recs)} rows; {suppressed} FT person(s) set to OptedOut (email+SMS).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
