"""Dry-run identity merge: how many 'regulars gone quiet' are really the same person under two records?

READ-ONLY. Writes nothing to the database. Re-counts visits after merging duplicate guest records with
conservative rules, and compares with the unmerged count.

Merge rules (a guest record = one email address in the Favourite Table export)
  A. Same email once an obvious domain typo is corrected (gmail.con -> gmail.com, stray spaces, etc.).
  B. Same mobile number AND same first name AND same last name (both present).
Not merged, only counted:
  C. Same mobile, different first name  -> household (couple, family, shared work phone).
  D. Email 1-2 letters different, or same email name on another provider -> left alone (could be two people).
Booking emails get the same typo correction before matching.

    railway run -s como-webhook python -m ingest.guest_merge --csv "<FT guest export>" --site Solihull [--list]

With --commit it SAVES the result into three tables of ours (no grants to dashboard_ro, nothing leaves Neon):
    public.guest_person    guest_id -> person_id
    public.person          one row per real person: consent (stricter rule: ANY opted-out record suppresses the
                           person on that channel), first name, postcode district, Como flag, household
    public.person_activity visits, no-shows, first/last visit, etc., counted per person after the merge
Re-running replaces all three. public.guest and public.guest_activity are left untouched.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import timedelta

from .config import settings
from .como import _hash, _norm_email
from .guest_activity import SQL_BOOKINGS, aggregate, build_maps, uk_today
from .guest_quality import POPULAR, lev, read_raw

PHONE_OK = ("valid_mobile", "mobile_in_tel_field")


def fix_domain(email):
    """Lower-case, drop whitespace, and repair an obvious domain typo. Returns the email unchanged if unsure."""
    e = re.sub(r"\s+", "", (email or "").lower())
    if "@" not in e:
        return e
    local, _, dom = e.rpartition("@")
    dom = dom.strip(".,;")
    if dom not in POPULAR:
        allow = 1 if len(dom) <= 11 else 2
        scored = sorted((lev(dom, p, allow), p) for p in POPULAR)
        scored = [(d, p) for d, p in scored if 0 < d <= allow]
        if scored and (len(scored) == 1 or scored[0][0] < scored[1][0]):
            dom = scored[0][1]
    return f"{local}@{dom}"


DDL = """
create table if not exists public.guest_person (
  guest_id   bigint primary key references public.guest(guest_id) on delete cascade,
  person_id  bigint not null
);
create index if not exists guest_person_person_idx on public.guest_person (person_id);
create table if not exists public.person (
  person_id         bigint primary key,
  records           int not null,
  email_consent     text,
  sms_consent       text,
  first_name        text,
  postcode_district text,
  in_como           boolean not null default false,
  household_id      bigint,
  household_size    int not null default 1,
  updated_at        timestamptz not null default now()
);
create table if not exists public.person_activity (
  person_id       bigint primary key references public.person(person_id) on delete cascade,
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
revoke all on public.guest_person, public.person, public.person_activity from public;
revoke all on public.guest_person, public.person, public.person_activity from dashboard_ro;
"""
SQL_GUESTS_FULL = ("select guest_id, email_hash, first_name, postcode_district, email_consent, sms_consent, in_como "
                   "from public.guest")


def combine_consent(values):
    """Stricter rule: any OptedOut suppresses the person; else any OptedIn; else NoConsent."""
    v = set(values)
    if "OptedOut" in v:
        return "OptedOut"
    if "OptedIn" in v:
        return "OptedIn"
    return "NoConsent"


def build_save(recs, keys, person, guest_rows, stats):
    """Pure function: returns the rows to write plus a summary Counter. No database access."""
    by = {g[1]: g for g in guest_rows if g[1]}
    members, unmapped = defaultdict(list), 0
    for k in keys:
        g = by.get(recs[k]["eh"])
        if g is None:
            unmapped += 1
        else:
            members[person[k]].append(g)
    pid_of_root = {root: min(x[0] for x in gs) for root, gs in members.items()}
    hh = DSU(max(pid_of_root.values(), default=0) + 1)
    by_phone = defaultdict(set)
    for k in keys:
        if recs[k]["phone_kind"] in PHONE_OK and person[k] in pid_of_root:
            by_phone[recs[k]["ph10"]].add(pid_of_root[person[k]])
    for pids in by_phone.values():
        pids = sorted(pids)
        for q in pids[1:]:
            hh.union(pids[0], q)
    comp = defaultdict(list)
    for pid in pid_of_root.values():
        comp[hh.find(pid)].append(pid)
    hh_id = {pid: min(c) for c in comp.values() if len(c) > 1 for pid in c}
    hh_size = {pid: len(c) for c in comp.values() if len(c) > 1 for pid in c}
    gp_rows, p_rows, sm = [], [], Counter()
    for root, gs in members.items():
        gs.sort(key=lambda x: x[0])
        pid = pid_of_root[root]
        gp_rows += [(g[0], pid) for g in gs]
        ec = combine_consent(g[4] for g in gs)
        sc = combine_consent(g[5] for g in gs)
        if ec == "OptedOut" and any(g[4] == "OptedIn" for g in gs):
            sm["email_suppressed_by_other_record"] += 1
        if sc == "OptedOut" and any(g[5] == "OptedIn" for g in gs):
            sm["sms_suppressed_by_other_record"] += 1
        p_rows.append((pid, len(gs), ec, sc, next((g[2] for g in gs if g[2]), None),
                       next((g[3] for g in gs if g[3]), None), any(bool(g[6]) for g in gs),
                       hh_id.get(pid), hh_size.get(pid, 1)))
    a_rows = []
    for s in stats:
        pid = pid_of_root.get(s["guest_id"])
        if pid is None:
            continue
        a_rows.append((pid, s["bookings_total"], s["visits"], s["noshows"], s["cancellations"],
                       s["past_unrecorded"], s["upcoming"], s["first_visit"], s["last_visit"],
                       s["next_booking"], s["last_site"], s["top_site"], s["avg_party"]))
    sm["unmapped_records"] = unmapped
    sm["persons"] = len(p_rows)
    sm["in_households"] = len(hh_id)
    return gp_rows, p_rows, a_rows, sm


def save(conn, gp_rows, p_rows, a_rows):
    from psycopg2.extras import execute_values
    with conn, conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute("delete from public.person_activity")
        cur.execute("delete from public.person")
        cur.execute("delete from public.guest_person")
        execute_values(cur, "insert into public.person (person_id, records, email_consent, sms_consent, first_name, "
                            "postcode_district, in_como, household_id, household_size) values %s", p_rows, page_size=5000)
        execute_values(cur, "insert into public.guest_person (guest_id, person_id) values %s", gp_rows, page_size=5000)
        execute_values(cur, "insert into public.person_activity (person_id, bookings_total, visits, noshows, "
                            "cancellations, past_unrecorded, upcoming, first_visit, last_visit, next_booking, "
                            "last_site, top_site, avg_party) values %s", a_rows, page_size=5000)


class DSU:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra
            return True
        return False


def cluster(recs):
    """recs: dict key->record. Returns (person_of {key: pid}, stats Counter, households int)."""
    keys = list(recs)
    idx = {k: i for i, k in enumerate(keys)}
    d, st = DSU(len(keys)), Counter()
    ce = {k: fix_domain(recs[k]["email"]) for k in keys}
    by_email = defaultdict(list)
    for k in keys:
        if ce[k]:
            by_email[ce[k]].append(k)
    for ks in by_email.values():
        for k in ks[1:]:
            if d.union(idx[ks[0]], idx[k]):
                st["rule_A_typo_email"] += 1
    by_pn = defaultdict(list)
    for k in keys:
        r = recs[k]
        if r["phone_kind"] in PHONE_OK and r["first"] and r["last"]:
            by_pn[(r["ph10"], r["first"].lower(), r["last"].lower())].append(k)
    for ks in by_pn.values():
        for k in ks[1:]:
            if d.union(idx[ks[0]], idx[k]):
                st["rule_B_phone_and_name"] += 1
    by_phone = defaultdict(set)
    for k in keys:
        if recs[k]["phone_kind"] in PHONE_OK:
            by_phone[recs[k]["ph10"]].add(d.find(idx[k]))
    households = sum(1 for s in by_phone.values() if len(s) > 1)
    return {k: d.find(idx[k]) + 1 for k in keys}, st, households, ce


def regulars(stats, site, cut):
    return {s["guest_id"] for s in stats if s["top_site"] == site and s["visits"] >= 2
            and s["last_visit"] and s["last_visit"] < cut and s["upcoming"] == 0}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--site", default="Solihull")
    ap.add_argument("--list", action="store_true", help="write the still-quiet regulars to a CSV in Downloads")
    ap.add_argument("--commit", action="store_true", help="save person tables (default is a dry run)")
    args = ap.parse_args(argv)
    if not settings.como_hash_salt:
        print("COMO_HASH_SALT is not set - refusing to run.", file=sys.stderr)
        return 2
    recs = read_raw(args.csv)
    keys = list(recs)
    person, st, households, ce = cluster(recs)
    solo = {k: i + 1 for i, k in enumerate(keys)}
    rows_solo = [(solo[k], recs[k]["eh"], recs[k]["phash"]) for k in keys]
    rows_merged = []
    for k in keys:
        rows_merged.append((person[k], recs[k]["eh"], recs[k]["phash"]))
        ceh = _hash(_norm_email(ce[k]))
        if ceh and ceh != recs[k]["eh"]:
            rows_merged.append((person[k], ceh, None))
    import psycopg2
    conn = psycopg2.connect(settings.database_url)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(SQL_BOOKINGS)
            bookings = cur.fetchall()
            guest_rows = []
            if args.commit:
                cur.execute(SQL_GUESTS_FULL)
                guest_rows = cur.fetchall()
    finally:
        conn.close()
    fixed = [(fix_domain(e) if e else e, t, bd, s, g, site) for e, t, bd, s, g, site in bookings]
    today = uk_today()
    cut = today - timedelta(days=90)
    s0, _ = aggregate(bookings, *build_maps(rows_solo), today)
    s1, c1 = aggregate(fixed, *build_maps(rows_merged), today)
    r0, r1 = regulars(s0, args.site, cut), regulars(s1, args.site, cut)
    a0 = sum(1 for s in s0 if s["visits"] >= 2 and s["last_visit"] and s["last_visit"] < cut and s["upcoming"] == 0)
    a1 = sum(1 for s in s1 if s["visits"] >= 2 and s["last_visit"] and s["last_visit"] < cut and s["upcoming"] == 0)
    persons = len(set(person.values()))
    print(f"as of {today} (UK). Guest records: {len(keys)}  ->  people after merge: {persons}")
    print(f"records merged: {len(keys) - persons}  (rule A typo-email: {st['rule_A_typo_email']}, "
          f"rule B phone+name: {st['rule_B_phone_and_name']})")
    print(f"households (same mobile, different people, NOT merged): {households}")
    print(f"guests with any booking: before {len(s0)}  ->  people with any booking after merge: {len(s1)}")
    print(f"{args.site} regulars gone quiet (2+ visits, last visit >90d ago, nothing booked): "
          f"before {len(r0)}  ->  after {len(r1)}")
    print(f"All sites: before {a0}  ->  after {a1}")
    if args.list:
        pk = {}
        for k in keys:
            pk.setdefault(person[k], []).append(k)
        by_p = {s["guest_id"]: s for s in s1}
        out = os.path.join(os.path.expanduser("~"), "Downloads", f"{args.site.lower()}-regulars-merged-{today}.csv")
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["name", "email", "visits", "last_visit", "records_merged", "also_known_as"])
            for pid in sorted(r1, key=lambda p: -by_p[p]["visits"]):
                ks = pk[pid]
                r = recs[ks[0]]
                w.writerow([f"{r['first']} {r['last']}".strip(), r["email"], by_p[pid]["visits"],
                            by_p[pid]["last_visit"], len(ks), "; ".join(recs[x]["email"] for x in ks[1:])])
        print(f"list written to: {out}  (personal data - keep it private)")
    if not args.commit:
        print("DRY RUN - nothing written to the database. Add --commit to save public.person tables.")
        return 0
    gp_rows, p_rows, a_rows, sm = build_save(recs, keys, person, guest_rows, s1)
    if sm["unmapped_records"] > 0.01 * len(keys):
        print(f"ABORTED: {sm['unmapped_records']} CSV records have no row in public.guest - reload the guest "
              f"table from this export first.", file=sys.stderr)
        return 3
    conn = psycopg2.connect(settings.database_url)
    try:
        save(conn, gp_rows, p_rows, a_rows)
    finally:
        conn.close()
    print(f"LOADED. public.person {len(p_rows)} people, public.guest_person {len(gp_rows)} links, "
          f"public.person_activity {len(a_rows)} people with bookings.")
    print(f"people in a shared-phone household: {sm['in_households']}")
    print(f"opted out by the stricter rule (another record of the same person had opted out): "
          f"email {sm['email_suppressed_by_other_record']}, sms {sm['sms_suppressed_by_other_record']}")
    print(f"CSV records with no guest row (skipped): {sm['unmapped_records']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
