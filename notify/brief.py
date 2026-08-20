#!/usr/bin/env python3
"""
Metabase-view -> ZenZap notifications.  `python -m notify.brief`

Runs every enabled rule in notify/config/alerts.yaml against Neon and sends one
message per site to that site's ZenZap group.

    python -m notify.brief                        # rules due this hour
    python -m notify.brief --rule daily-site-brief   # one rule, any hour
    python -m notify.brief --dry-run              # print, send nothing
    python -m notify.brief --list

Deployment
----------
ONE Railway service (railway.brief.json) wakes every hour and runs whatever
rules are due that hour, per each rule's own `schedule:` in alerts.yaml.

That means a new message at a new time is a CONFIG change — add the rule with
the hour you want. No new service, no Railway change, no deploy beyond the
push.

Environment (Railway service variables)
---------------------------------------
    DATABASE_URL        already used by the ingest service
    ZENZAP_API_KEY
    ZENZAP_API_SECRET
    ZENZAP_BASE_URL     https://api.zenzap.co

Clocks
------
Railway cron is UTC and ignores British Summer Time, so a fixed "0 11 * * *"
on the service would drift to midday every October. The service instead runs
hourly and each rule's schedule is evaluated in Europe/London, so 11:00 means
11:00 all year with nothing to remember at the clock change.

Safety
------
  * --dry-run prints and sends nothing.
  * Refuses to send if more sites breach than max_sites_per_run — that pattern
    means a broken query far more often than five genuinely bad days.
  * Deterministic message externalIds: a re-run on the same day cannot
    double-post, so a late or repeated run is always safe.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from notify import db, sport
from notify.alerts import AlertConfigError, Rule, load_all, max_sites_per_run
from notify.sites import registry, in_quiet_hours
from notify.zenzap import ZenZapClient, ZenZapError

log = logging.getLogger("notify.brief")
LONDON = ZoneInfo("Europe/London")


def _reminder_rows(rule: Rule, sites) -> list[dict]:
    """A reminder has no query, so build one row per targeted site.

    `sites:` on the rule narrows it; omitted means every site on ZenZap.
    An unknown key is an error rather than a silent no-op — a typo'd site key
    would otherwise mean the reminder just never arrives and nobody notices.
    """
    targets = sites.on_zenzap()
    if rule.sites:
        wanted = set(rule.sites)
        targets = [s for s in targets if s.key in wanted]
        unknown = wanted - {s.key for s in sites.all()}
        if unknown:
            raise AlertConfigError(
                f"Reminder '{rule.key}' targets unknown site key(s): {sorted(unknown)}"
            )
    return [{"location_id": s["lightspeed_location_id"]} for s in targets]


def _sport_block(cache: dict) -> str:
    """Today's fixtures, fetched at most once per run.

    All five sites share FANZO venue 17079, so the list is identical
    everywhere — fetching it per site would be four wasted round trips
    against someone else's undocumented endpoint.
    """
    if "text" not in cache:
        cache["text"] = sport.block()
    return cache["text"]


def run_rule(rule: Rule, sites, zz, args, sport_cache=None) -> tuple[int, int]:
    kind = "reminder" if rule.is_reminder else "alert"
    print(f"\n=== {rule.key} — {rule.title}  [{kind}] ===")

    if rule.is_reminder:
        try:
            rows = _reminder_rows(rule, sites)
        except AlertConfigError as exc:
            print(f"  ✗ {exc}")
            return 0, 1
    else:
        try:
            rows = db.query(rule.sql)
        except db.NotifyDBError as exc:
            print(f"  ✗ query failed: {exc}")
            return 0, 1

    breaching = [r for r in rows if rule.matches(r)]
    print(f"  {len(rows)} rows, {len(breaching)} to send")

    if not breaching:
        return 0, 0

    cap = max_sites_per_run()
    if len(breaching) > cap:
        print(f"  ✗ REFUSING TO SEND: {len(breaching)} exceeds cap of {cap}.")
        print("    Investigate the query before overriding — this is almost")
        print("    always a broken filter, not five bad nights.")
        return 0, 1

    sent = failed = 0
    now = datetime.now(LONDON)

    for row in breaching:
        # location_id is the reliable key; site name is the fallback. Names
        # differ across sources ("Solihull" in Nory/bookings, "Tap Solihull"
        # in the POS views, "Tap Bournemouth." with a trailing full stop).
        site = None
        if row.get("location_id"):
            site = sites.by_location_id(row["location_id"])
        site_name = row.get(rule.site_column)
        if site is None and site_name:
            site = sites.by_nory_name(site_name)

        if site is None:
            print(f"  ! '{site_name or row.get('location_id')}' not in sites.yaml — skipped")
            failed += 1
            continue

        text = rule.render(row, site.name)

        if rule.sport_block:
            block = _sport_block(sport_cache if sport_cache is not None else {})
            if block:
                text = f"{text}\n\n{block}"

        if rule.route.startswith("group:"):
            group = rule.route.split(":", 1)[1]
            topic = sites.group_topic(group)
            if not topic:
                print(f"  ✗ route '{rule.route}' — no such group in sites.yaml")
                failed += 1
                continue
            ext_id = f"{rule.key}-{now:%Y%m%d}-{site.key}"[:61]
            if args.dry_run:
                print(f"\n  --- would send to group '{topic}' ---")
                print("  " + text.replace("\n", "\n  "))
                sent += 1
                continue
            try:
                zz.send_message(text, topic_name=topic, external_id=ext_id)
                print(f"  ✓ {site.name} -> {topic}")
                sent += 1
            except ZenZapError as exc:
                print(f"  ✗ {site.name} -> {topic}: {exc}")
                failed += 1
            continue

        if rule.route == "none":
            print(f"\n  --- {site.name} (route: none, not sent) ---")
            print("  " + text.replace("\n", "\n  "))
            continue

        if not site.uses_zenzap:
            print(f"  · {site.name}: not on ZenZap — no message")
            continue

        if not args.ignore_quiet_hours and in_quiet_hours(site, now):
            print(f"  · {site.name}: inside quiet hours — held")
            continue

        ext_id = f"{rule.key}-{now:%Y%m%d}-{site.key}"[:61]

        if args.dry_run:
            print(f"\n  --- would send to '{site.topic}' ---")
            print("  " + text.replace("\n", "\n  "))
            sent += 1
            continue

        try:
            zz.send_message(text, topic_name=site.topic, external_id=ext_id)
            print(f"  ✓ {site.name}")
            sent += 1
        except ZenZapError as exc:
            print(f"  ✗ {site.name}: {exc}")
            failed += 1

    return sent, failed


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--rule", help="run a single rule by key")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--ignore-quiet-hours", action="store_true")
    args = parser.parse_args()

    rules = load_all()

    if args.list:
        for kind, group in (("ALERTS (data-driven)", [r for r in rules if not r.is_reminder]),
                            ("REMINDERS (time-driven)", [r for r in rules if r.is_reminder])):
            print(f"\n{kind}")
            if not group:
                print("  (none)")
            for rule in group:
                state = "on " if rule.enabled else "off"
                who = ",".join(rule.sites) if rule.sites else "all sites"
                print(f"  [{state}] {rule.key:<26} {rule.title}")
                print(f"        {rule.schedule or 'manual only':<16} -> {rule.route:<22} {who}")
        return 0

    now = datetime.now(LONDON)

    if args.rule:
        # Explicit rule: run it regardless of schedule or hour.
        selected = [r for r in rules if r.key == args.rule]
        if not selected:
            print(f"No rule '{args.rule}'. Available: {[r.key for r in rules]}")
            return 1
    else:
        # Unattended: the service wakes hourly and runs whatever is due now.
        selected = [r for r in rules if r.enabled and r.due_now(now)]
        if not selected:
            print(f"{now:%a %d %b %H:%M} — no rules due this hour.")
            return 0
        print(f"{now:%a %d %b %H:%M} — due now: {[r.key for r in selected]}")

    sites = registry()
    zz = None if args.dry_run else ZenZapClient()

    total_sent = total_failed = 0
    sport_cache: dict = {}
    for rule in selected:
        sent, failed = run_rule(rule, sites, zz, args, sport_cache)
        total_sent += sent
        total_failed += failed

    verb = "would send" if args.dry_run else "sent"
    print(f"\n{len(selected)} rule(s): {total_sent} {verb}, {total_failed} failed")
    return 1 if total_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
