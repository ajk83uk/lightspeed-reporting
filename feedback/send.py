"""Daily job: create survey requests for yesterday's dining guests and email them.

SAFE BY DEFAULT. With no flags this is a dry run: it renders every email to previews/, writes nothing to
the database (the transaction is rolled back) and sends nothing. It only sends for real when BOTH
FEEDBACK_SEND_ENABLED=true and --live are given, and a provider is configured.

  python -m feedback.send                      dry run for yesterday (UK date)
  python -m feedback.send --date 2026-11-26    dry run for a chosen day
  python -m feedback.send --site Solihull      one site only
  python -m feedback.send --persist-dry-run    keep dry-run rows so preview links open the real survey
  python -m feedback.send --purge-dry-runs     delete unsent dry-run rows
  python -m feedback.send --live               real send (needs FEEDBACK_SEND_ENABLED=true)
"""
import argparse
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras

from . import config, mailer
from .db import q

NAME_TO_LOC = {v["name"]: k for k, v in config.SITES.items()}

CANDIDATES = """
select c.* from v_feedback_candidates c
where c.visit_date = %(vd)s
  and (%(loc)s::bigint is null or c.business_location_id = %(loc)s)
  and not exists (                      -- do not email the same person twice in 14 days
    select 1 from feedback_requests r2
    join bookings b2 on b2.business_location_id = r2.business_location_id
                    and b2.booking_ref_no = r2.booking_ref_no and b2.booking_date = r2.visit_date
    where lower(b2.email) = lower(c.email) and r2.dry_run = false
      and r2.status in ('sent','opened','completed') and r2.sent_at > now() - interval '14 days')
order by c.business_location_id, c.booking_ref_no
"""

INSERT = """
insert into feedback_requests (business_location_id, ft_site_code, booking_ref_no, booking_code, visit_date, covers, dry_run, status)
values (%(loc)s, %(ft)s, %(ref)s, %(code)s, %(vd)s, %(covers)s, %(dry)s, 'queued')
on conflict (business_location_id, booking_ref_no, visit_date) do nothing
returning token
"""


def uk_yesterday() -> date:
    return (datetime.now(ZoneInfo("Europe/London")) - timedelta(days=1)).date()


def nice_date(d: date) -> str:
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B')}"


def run(conn, *, visit_date, site=None, limit=300, live=False, persist_dry_run=False, provider=None):
    loc = NAME_TO_LOC[site] if site else None
    rows = q(conn, CANDIDATES, {"vd": visit_date, "loc": loc}).fetchall()
    seen, results, handled = set(), Counter(), 0
    for row in rows:
        if handled >= limit:
            results["capped"] += 1
            continue
        email = row["email"].strip().lower()
        if email in seen:
            results["duplicate_email"] += 1
            continue
        seen.add(email)
        made = q(conn, 
            INSERT,
            {"loc": row["business_location_id"], "ft": row["ft_site_code"], "ref": row["booking_ref_no"],
             "code": row["booking_code"], "vd": row["visit_date"], "covers": row["covers"], "dry": not live},
        ).fetchone()
        if not made:
            results["already_requested"] += 1
            continue
        token = made["token"]
        if live:
            conn.commit()  # persist the request before sending so a crash can never cause a second email
        site_cfg = config.SITES[int(row["business_location_id"])]
        msg = mailer.build_message(
            token=token, to_email=row["email"].strip(), first_name=row["first_name"],
            site=site_cfg, visit_date_text=nice_date(row["visit_date"]),
        )
        try:
            provider.send(msg, key=f"{site_cfg['name'].lower()}-{row['booking_ref_no']}")
        except Exception as exc:  # noqa: BLE001 - one bad send must not stop the run
            results[f"failed:{site_cfg['name']}"] += 1
            handled += 1
            if live:
                q(conn, "update feedback_requests set status='failed' where token=%s", (token,))
                conn.commit()
            print(f"send failed for {site_cfg['name']} booking {row['booking_ref_no']}: {type(exc).__name__}", file=sys.stderr)
            continue
        results[site_cfg["name"]] += 1
        handled += 1
        if live:
            q(conn, "update feedback_requests set status='sent', sent_at=now() where token=%s", (token,))
            conn.commit()
    if not live and not persist_dry_run:
        conn.rollback()
    else:
        conn.commit()
    return dict(results)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--date")
    p.add_argument("--site", choices=sorted(NAME_TO_LOC))
    p.add_argument("--limit", type=int, default=300)
    p.add_argument("--live", action="store_true")
    p.add_argument("--persist-dry-run", action="store_true")
    p.add_argument("--purge-dry-runs", action="store_true")
    p.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "previews"))
    a = p.parse_args(argv)

    psycopg2.extras.register_uuid()
    conn = psycopg2.connect(config.database_url(), cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        if a.purge_dry_runs:
            n = q(conn, 
                "with d as (delete from feedback_requests where dry_run = true and status = 'queued' and token not in "
                "(select token from feedback_responses) returning 1) select count(*) as n from d"
            ).fetchone()["n"]
            conn.commit()
            print(f"purged {n} unsent dry-run rows")
            conn.close()
            return 0
        live = a.live and os.environ.get("FEEDBACK_SEND_ENABLED", "").lower() == "true"
        if a.live and not live:
            print("Refusing to send: --live needs FEEDBACK_SEND_ENABLED=true. Running as a dry run instead.", file=sys.stderr)
        try:
            provider = mailer.get_provider(live, a.out)
        except mailer.ProviderNotConfigured as exc:
            print(f"Cannot send: {exc}", file=sys.stderr)
            return 2
        vd = date.fromisoformat(a.date) if a.date else uk_yesterday()
        out = run(conn, visit_date=vd, site=a.site, limit=a.limit, live=live,
                  persist_dry_run=a.persist_dry_run, provider=provider)
    finally:
        conn.close()
    mode = "LIVE" if live else "DRY RUN (nothing sent)"
    print(f"{mode} for {vd}: {out or 'no candidates'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
