"""
Alert engine tests, using REAL rows pulled from nory_labour_daily (neondb)
on 2026-08-19. Fixtures are genuine data, so a threshold change shows its
true effect on your sites rather than on invented numbers.

    python -m pytest tests/test_alerts.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from notify.alerts import AlertConfigError, load_rules  # noqa: E402
from notify.sites import registry  # noqa: E402


def _row(site, biz_date, hours, planned):
    var = round(hours - planned, 1)
    return {
        "site_name": site,
        "biz_date": biz_date,
        "hours": hours,
        "planned_hours": planned,
        "hours_var": var,
        "pct_var": round((var / planned) * 100, 1) if planned else None,
    }


# Real hours from nory_labour_daily, 11-18 Aug 2026.
REAL = {
    "2026-08-18": [("Bournemouth", 28, 31.2), ("Peterborough", 32.5, 39.8),
                   ("Portsmouth", 37.9, 38.8), ("Solihull", 52, 51.5),
                   ("Southampton", 33.2, 38.5)],
    "2026-08-17": [("Bournemouth", 30.1, 29.8), ("Peterborough", 44.8, 37.5),
                   ("Portsmouth", 45.6, 42.8), ("Solihull", 49.9, 43),
                   ("Southampton", 42.4, 32.8)],
    "2026-08-16": [("Bournemouth", 35.6, 42.5), ("Peterborough", 54.1, 47.5),
                   ("Portsmouth", 66.1, 46.5), ("Solihull", 67.8, 61),
                   ("Southampton", 37.3, 41)],
    "2026-08-15": [("Bournemouth", 56.8, 45.8), ("Peterborough", 81.2, 83.5),
                   ("Portsmouth", 76.1, 82), ("Solihull", 80.3, 91),
                   ("Southampton", 51.2, 55)],
}


def rows_for(date: str) -> list[dict]:
    return [_row(s, date, h, p) for s, h, p in REAL[date]]


@pytest.fixture
def hours_rule():
    return next(r for r in load_rules() if r.key == "labour-hours-over")


# ------------------------------------------------------------------ behaviour

def test_config_loads_and_keys_are_unique():
    rules = load_rules()
    assert {r.key for r in rules} == {
        "daily-site-brief", "labour-hours-over", "labour-hours-saved",
        "labour-pct-over-target", "nory-sales-not-syncing",
    }


def test_labour_pct_rule_is_disabled():
    """Must stay off until the Nory sales feed is fixed. Two sites report
    sales = 0 on every row, so a % rule is silently blind to them."""
    pct = next(r for r in load_rules() if r.key == "labour-pct-over-target")
    assert pct.enabled is False


def test_real_data_18_aug_fires_for_nobody(hours_rule):
    """Solihull was the only site over on hours (+0.5), below both thresholds."""
    breaching = [r for r in rows_for("2026-08-18") if hours_rule.matches(r)]
    assert breaching == []


def test_real_data_17_aug_fires_for_four_sites(hours_rule):
    breaching = {r["site_name"] for r in rows_for("2026-08-17") if hours_rule.matches(r)}
    assert breaching == {"Peterborough", "Portsmouth", "Solihull", "Southampton"}
    # Bournemouth was +0.3 hrs — correctly ignored.
    assert "Bournemouth" not in breaching


def test_real_data_16_aug_catches_the_big_one(hours_rule):
    breaching = {r["site_name"] for r in rows_for("2026-08-16") if hours_rule.matches(r)}
    assert "Portsmouth" in breaching      # +19.6 hrs, the worst in the window
    assert "Southampton" not in breaching  # -3.7, under


def test_underruns_never_fire(hours_rule):
    """Being under schedule is a different conversation and must not alert."""
    for date in REAL:
        for row in rows_for(date):
            if row["hours_var"] < 0:
                assert not hours_rule.matches(row), row


def test_alert_volume_is_sustainable(hours_rule):
    """Across 4 real days, 5 sites: if this fires on most site-days the
    thresholds are wrong and staff will learn to ignore it."""
    total = sum(1 for d in REAL for r in rows_for(d) if hours_rule.matches(r))
    site_days = sum(len(REAL[d]) for d in REAL)
    assert 0 < total < site_days * 0.5, f"{total}/{site_days} — too noisy"


# ------------------------------------------------------------------- matching

def test_missing_column_never_matches(hours_rule):
    """A broken query must not be read as a breach."""
    assert not hours_rule.matches({"site_name": "Solihull"})


def test_none_never_matches(hours_rule):
    """None means 'no data', not zero. Must not silently pass or fail open."""
    assert not hours_rule.matches(
        {"site_name": "Solihull", "hours_var": None, "pct_var": None}
    )


def test_both_thresholds_required(hours_rule):
    # 10 hrs over but only 2% — big site, proportionally fine.
    assert not hours_rule.matches({"hours_var": 10.0, "pct_var": 2.0})
    # 20% over but only 1 hr — small shift, not worth a message.
    assert not hours_rule.matches({"hours_var": 1.0, "pct_var": 20.0})
    assert hours_rule.matches({"hours_var": 5.0, "pct_var": 12.0})


# ------------------------------------------------------------------ rendering

def test_message_renders_with_real_row(hours_rule):
    row = _row("Solihull", "2026-08-17", 49.9, 43)
    text = hours_rule.render(row, "Tap & Tandoor Solihull")

    assert "Tap & Tandoor Solihull" in text
    assert "Mon 17 Aug" in text          # {biz_date:date} formatting
    assert "6.9 hrs" in text
    assert "{" not in text               # nothing left unsubstituted


def test_unresolvable_placeholder_raises():
    """Better to fail the job than post '{covers}' into a staff group."""
    rule = next(r for r in load_rules() if r.key == "labour-hours-over")
    rule.message = "Value: {column_that_does_not_exist}"
    with pytest.raises(AlertConfigError, match="doesn't return"):
        rule.render({"site_name": "Solihull"}, "Solihull")


# -------------------------------------------------------------------- routing

def test_praise_and_criticism_use_the_same_bar():
    """If praise were easier to earn than criticism, it would be noise.
    Both rules must use identical thresholds, just inverted."""
    rules = {r.key: r for r in load_rules()}
    over = {(c["column"].replace("_var", ""), c["value"])
            for c in rules["labour-hours-over"].condition["all"]}
    under = {(c["column"].replace("_saved", ""), c["value"])
             for c in rules["labour-hours-saved"].condition["all"]}
    assert {v for _, v in over} == {v for _, v in under}


def test_brief_always_matches():
    """A brief is not conditional — every site gets one every day."""
    brief = next(r for r in load_rules() if r.key == "daily-site-brief")
    assert brief.is_brief
    assert brief.matches({})
    assert brief.matches({"covers": None})


def test_brief_renders_from_real_row():
    brief = next(r for r in load_rules() if r.key == "daily-site-brief")
    # Real Solihull row, 18 Aug 2026.
    row = dict(location_id="1718940401139720", business_date="2026-08-18",
               today="2026-08-19", covers=86, apc=30.31, wet_pct=34, dry_pct=66,
               dpc=2.14, covers_lw=83, act_hours=52.0, sch_hours=51.5,
               hrs_var=0.5, hrs_abs=0.5, hrs_dir="over", money_abs=8,
               money_dir="cost", booked_today=20, bookings_today=5,
               exp_walkins=57, typical_total=108, expect_total=77, rota_note="")
    text = brief.render(row, "Tap & Tandoor Solihull")
    assert "{" not in text
    assert "Booked: 20 covers" in text
    assert "Dine-in covers" in text          # must be explicit about the basis
    assert "Dine-in spend per head" in text
    assert "34% / 66%" in text
    # Last night must come first — it is what earns their attention.
    assert text.index("Last night") < text.index("Today")
    # Hours line must agree with itself: 0.5 over, not 0.5 under.
    assert "0.5 over" in text
    # Sales value deliberately absent.
    assert "ex VAT" not in text
    # Must be short enough to read on a phone mid-shift.
    assert len(text.splitlines()) <= 16, "brief is getting too long"


def test_money_is_derived_from_hours_not_the_cost_columns():
    """Nory's planned_col is a different cost basis (£19.70-£25.68/hr) to
    actual (£14.76-£16.14/hr) — almost certainly fully loaded. Subtracting
    them shows every site saving money every night, including Solihull on
    18 Aug when it worked 0.5 hrs OVER. The £ figure must therefore follow
    the sign of the hours variance, always."""
    brief = next(r for r in load_rules() if r.key == "daily-site-brief")
    assert "planned_col" not in brief.sql, "must not use planned_col for variance"
    assert "n.col / NULLIF(n.hours,0)" in brief.sql, "£ must derive from actual rate"

    over = dict(location_id="1718940401139720", business_date="2026-08-18",
                today="2026-08-19", covers=86, apc=30.31, wet_pct=34, dry_pct=66,
                dpc=2.14, covers_lw=83, act_hours=52.0, sch_hours=51.5,
                hrs_var=0.5, hrs_abs=0.5, hrs_dir="over", money_abs=8,
                money_dir="cost", booked_today=20, bookings_today=5,
                exp_walkins=57, typical_total=108, expect_total=77, rota_note="")
    text = brief.render(over, "Solihull")
    assert "over" in text and "cost" in text
    assert "saved" not in text, "worked over schedule but message says saved"


def test_brief_resolves_by_location_id_not_name():
    """Names differ across sources; the id is the only stable key."""
    sites = registry()
    assert sites.by_location_id("1718940401139720").key == "tt-solihull"
    assert sites.by_location_id("1718940401139714").key == "tt-bournemouth"
    assert sites.by_location_id("nope") is None


def test_every_rule_routes_somewhere_valid():
    for rule in load_rules():
        assert rule.route in {"site", "none"}, f"{rule.key}: bad route {rule.route}"


def test_breaching_sites_all_resolve_to_real_sites(hours_rule):
    """Every Nory site name the query can return must map to sites.yaml,
    or the alert silently vanishes."""
    sites = registry()
    for date in REAL:
        for row in rows_for(date):
            assert sites.by_nory_name(row["site_name"]) is not None, row["site_name"]


# ------------------------------------------------------------- wednesday nudge

def _brief_row(**over):
    row = dict(location_id="1718940401139720", business_date="2026-08-18",
               today="2026-08-19", covers=86, apc=30.31, wet_pct=34, dry_pct=66,
               dpc=2.14, covers_lw=83, act_hours=52.0, sch_hours=51.5,
               hrs_var=0.5, hrs_abs=0.5, hrs_dir="over", money_abs=8,
               money_dir="cost", booked_today=20, bookings_today=5,
               exp_walkins=57, typical_total=108, expect_total=77, rota_note="")
    row.update(over)
    return row


def test_rota_nudge_is_wednesday_only_in_sql():
    """ISODOW 3 = Wednesday. Guards against an off-by-one that would put the
    rota reminder out on Tuesday or Thursday every week."""
    brief = next(r for r in load_rules() if r.key == "daily-site-brief")
    assert "EXTRACT(ISODOW FROM CURRENT_DATE) = 3" in brief.sql
    assert "planning your rotas" in brief.sql


def test_brief_renders_cleanly_without_the_nudge():
    """Six days a week rota_note is empty — no stray blank block or
    leftover placeholder."""
    brief = next(r for r in load_rules() if r.key == "daily-site-brief")
    text = brief.render(_brief_row(), "Tap & Tandoor Solihull")
    assert "{" not in text
    assert "Wednesday" not in text
    assert not text.endswith("\n")


def test_brief_renders_the_nudge_on_wednesday():
    brief = next(r for r in load_rules() if r.key == "daily-site-brief")
    note = "\n\n\U0001F4CB It is Wednesday today. Please start planning your rotas for the following week."
    text = brief.render(_brief_row(rota_note=note), "Tap & Tandoor Solihull")
    assert "It is Wednesday today" in text
    assert "{" not in text
    # Must sit at the end, after today's numbers — not interrupting them.
    assert text.index("Wednesday") > text.index("Expect around")


def test_brief_scheduled_for_11am_daily():
    brief = next(r for r in load_rules() if r.key == "daily-site-brief")
    assert brief.schedule == "0 11 * * *"



# ------------------------------------------------------ dine-in consistency

def test_covers_and_apc_share_one_basis():
    """Covers x spend-per-head must equal dining revenue.

    v_site_day.covers and v_site_day_apc.covers_clean disagree (124 vs 120 at
    Solihull on 19 Aug 2026) because they count differently. Mixing them means
    the arithmetic in the message doesn't tie, and a GM who spots that stops
    trusting every other figure. Both must come from covers_clean."""
    brief = next(r for r in load_rules() if r.key == "daily-site-brief")
    assert "a.covers_clean               AS covers" in brief.sql
    assert "covers_clean AS covers_lw" in brief.sql
    assert "SELECT business_location_id AS loc, covers AS covers_lw" not in brief.sql


def test_apc_is_the_dine_in_measure():
    """apc_dining excludes over-the-bar; apc_raw does not. Never swap them."""
    brief = next(r for r in load_rules() if r.key == "daily-site-brief")
    assert "a.apc_dining" in brief.sql
    assert "apc_raw" not in brief.sql


# --------------------------------------------------------------- scheduling

def _rule(schedule):
    r = next(x for x in load_rules() if x.key == "daily-site-brief")
    r.schedule = schedule
    return r


def test_rule_fires_only_in_its_own_hour():
    """One service runs hourly and asks each rule if it's due. Get this wrong
    and every message goes out at every hour."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    london = ZoneInfo("Europe/London")
    r = _rule("0 11 * * *")

    assert r.due_now(datetime(2026, 8, 20, 11, 0, tzinfo=london)) is True
    assert r.due_now(datetime(2026, 8, 20, 10, 0, tzinfo=london)) is False
    assert r.due_now(datetime(2026, 8, 20, 12, 0, tzinfo=london)) is False


def test_schedule_follows_the_clocks():
    """Evaluated in Europe/London, so 11:00 is 11:00 in BST and in GMT.
    A UTC-based check would drift an hour every October."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    london = ZoneInfo("Europe/London")
    r = _rule("0 11 * * *")

    summer = datetime(2026, 8, 20, 11, 0, tzinfo=london)   # BST (UTC+1)
    winter = datetime(2026, 12, 10, 11, 0, tzinfo=london)  # GMT (UTC+0)
    assert r.due_now(summer) is True
    assert r.due_now(winter) is True
    assert summer.utcoffset() != winter.utcoffset()        # genuinely different


def test_weekday_only_schedules():
    """`0 9 * * 1` = Mondays. Used for weekly summaries."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    london = ZoneInfo("Europe/London")
    monday = _rule("0 9 * * 1")

    assert monday.due_now(datetime(2026, 8, 24, 9, 0, tzinfo=london)) is True   # Mon
    assert monday.due_now(datetime(2026, 8, 25, 9, 0, tzinfo=london)) is False  # Tue

    weekdays = _rule("0 9 * * 1-5")
    assert weekdays.due_now(datetime(2026, 8, 21, 9, 0, tzinfo=london)) is True   # Fri
    assert weekdays.due_now(datetime(2026, 8, 22, 9, 0, tzinfo=london)) is False  # Sat

    sunday = _rule("0 9 * * 0")
    assert sunday.due_now(datetime(2026, 8, 23, 9, 0, tzinfo=london)) is True    # Sun


def test_rule_without_schedule_never_auto_runs():
    """No schedule means manual-only — must not fire every hour."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    r = _rule(None)
    assert r.due_now(datetime(2026, 8, 20, 11, 0, tzinfo=ZoneInfo("Europe/London"))) is False


def test_malformed_schedule_raises_rather_than_guessing():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    r = _rule("11am please")
    with pytest.raises(AlertConfigError, match="5 cron fields"):
        r.due_now(datetime(2026, 8, 20, 11, 0, tzinfo=ZoneInfo("Europe/London")))


def test_every_enabled_rule_has_a_usable_schedule():
    """An enabled rule with no or broken schedule would silently never send."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    london = ZoneInfo("Europe/London")
    for rule in load_rules():
        if not rule.enabled:
            continue
        assert rule.schedule, f"{rule.key} is enabled but has no schedule"
        fires = any(
            rule.due_now(datetime(2026, 8, 24, h, 0, tzinfo=london))
            for h in range(24)
        )
        assert fires, f"{rule.key} never fires on a Monday — check its schedule"


# ------------------------------------------------------------------ routing

def test_routes_are_valid_and_groups_exist():
    """route: group:<name> must name a real group, or the message vanishes."""
    sites = registry()
    for rule in load_rules():
        if rule.route.startswith("group:"):
            name = rule.route.split(":", 1)[1]
            assert sites.group_topic(name), f"{rule.key}: no group '{name}'"
        else:
            assert rule.route in {"site", "none"}, f"{rule.key}: bad route"


# ----------------------------------------------------------------- reminders

def test_reminders_need_no_sql():
    """A reminder fires on time and says something — there is nothing to query.
    Requiring SQL would force fake queries for pure prompts."""
    from notify.alerts import load_reminders
    for r in load_reminders():
        assert r.is_reminder
        assert r.sql is None
        assert r.message.strip()


def test_alerts_are_not_mistaken_for_reminders():
    for r in load_rules():
        assert not r.is_reminder, f"{r.key} lost its SQL"


def test_keys_unique_across_both_files():
    """The key is the idempotency handle on every message. A key reused in
    alerts.yaml and reminders.yaml would let one suppress the other."""
    from notify.alerts import load_all
    keys = [r.key for r in load_all()]
    assert len(keys) == len(set(keys))


def test_reminder_targets_resolve_to_real_sites():
    """A typo'd site key would mean the reminder silently never arrives."""
    from notify.alerts import load_reminders
    sites = registry()
    valid = {s.key for s in sites.all()}
    for r in load_reminders():
        for key in (r.sites or []):
            assert key in valid, f"{r.key}: unknown site '{key}'"


def test_reminder_rows_default_to_every_zenzap_site():
    from notify.alerts import load_reminders
    from notify.brief import _reminder_rows
    sites = registry()
    r = next(x for x in load_reminders() if x.key == "wednesday-rota-prompt")
    assert len(_reminder_rows(r, sites)) == len(sites.on_zenzap()) == 5


def test_reminder_can_target_one_site():
    from notify.alerts import load_reminders
    from notify.brief import _reminder_rows
    sites = registry()
    r = next(x for x in load_reminders() if x.key == "bournemouth-bins-thursday")
    rows = _reminder_rows(r, sites)
    assert len(rows) == 1
    assert sites.by_location_id(rows[0]["location_id"]).key == "tt-bournemouth"


def test_example_reminders_ship_disabled():
    """The examples in reminders.yaml are documentation. If they shipped
    enabled, five sites would get a cellar-clean prompt nobody asked for."""
    from notify.alerts import load_reminders
    for r in load_reminders():
        if r.key.startswith("pipeline-test-"):
            continue          # deliberate one-off, dated, deletes itself by date
        assert r.enabled is False, f"{r.key} is an example and must ship off"


def test_quiet_hours_would_hold_an_early_reminder():
    """A 07:00 reminder would be silently held by the 23:00-08:00 window.
    This is documented in reminders.yaml; the test proves the behaviour so
    nobody schedules a 6am prompt and wonders why it never arrives."""
    from datetime import datetime
    from notify.sites import in_quiet_hours
    site = registry().by_key("tt-bournemouth")
    assert in_quiet_hours(site, datetime(2026, 8, 20, 7, 0))      # held
    assert not in_quiet_hours(site, datetime(2026, 8, 20, 21, 0))  # bins: fine
    assert not in_quiet_hours(site, datetime(2026, 8, 20, 8, 0))   # earliest ok


def test_bins_reminder_is_the_night_before_collection():
    """Wednesday 21:00 for a Thursday collection. An off-by-one on the day
    means the bins go out the night after they were collected."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from notify.alerts import load_reminders
    london = ZoneInfo("Europe/London")
    r = next(x for x in load_reminders() if x.key == "bournemouth-bins-thursday")

    assert r.due_now(datetime(2026, 8, 19, 21, 0, tzinfo=london)) is True   # Wed
    assert r.due_now(datetime(2026, 8, 20, 21, 0, tzinfo=london)) is False  # Thu
    assert r.due_now(datetime(2026, 8, 19, 20, 0, tzinfo=london)) is False  # 8pm
    assert r.sites == ["tt-bournemouth"]


# ------------------------------------------------- pasted-credential hygiene

def test_trailing_newline_in_env_is_stripped(monkeypatch):
    """Railway (and every other hosting panel) happily stores a trailing
    newline when you paste a value. Symptoms are baffling:
      base_url   -> DNS lookup for 'api.zenzap.co%0a'
      api_secret -> HMAC over wrong bytes, silent 401
    This cost a live 11:00 run on 20 Aug 2026. Strip everything."""
    from notify.zenzap import ZenZapClient

    monkeypatch.setenv("ZENZAP_BASE_URL", "https://api.zenzap.co\n")
    monkeypatch.setenv("ZENZAP_API_KEY", "  abc123  ")
    monkeypatch.setenv("ZENZAP_API_SECRET", "secret\r\n")

    c = ZenZapClient()
    assert c.base_url == "https://api.zenzap.co"
    assert "\n" not in c.base_url and "%0a" not in c.base_url
    assert c.api_key == "abc123"
    assert c.api_secret == "secret"


def test_trailing_slash_on_base_url_is_trimmed(monkeypatch):
    """Otherwise every request URL contains a double slash."""
    from notify.zenzap import ZenZapClient
    monkeypatch.setenv("ZENZAP_BASE_URL", "https://api.zenzap.co/")
    assert ZenZapClient().base_url == "https://api.zenzap.co"


def test_network_failure_is_a_zenzap_error(monkeypatch):
    """A raw requests exception used to escape and abort the whole run on the
    first site, so sites 2-5 silently got nothing. It must be catchable so the
    loop can record that site as failed and carry on."""
    import requests
    from notify.zenzap import ZenZapClient, ZenZapError

    def boom(*a, **kw):
        raise requests.exceptions.ConnectionError("dns is having a day")

    monkeypatch.setenv("ZENZAP_API_KEY", "k")
    monkeypatch.setenv("ZENZAP_API_SECRET", "s")
    monkeypatch.setattr(requests, "request", boom)

    with pytest.raises(ZenZapError, match="unreachable"):
        ZenZapClient().send_message("hi", topic_id="abc", external_id="x")


def test_day_of_month_is_honoured():
    """The parser originally read the day-of-month field and ignored it, so
    "0 10 1 * *" (1st of the month) would have fired at 10:00 EVERY day.
    Caught before crockery-count-monthly was ever enabled."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    london = ZoneInfo("Europe/London")
    r = _rule("0 10 1 * *")

    assert r.due_now(datetime(2026, 9, 1, 10, 0, tzinfo=london)) is True    # 1st
    assert r.due_now(datetime(2026, 9, 2, 10, 0, tzinfo=london)) is False   # 2nd
    assert r.due_now(datetime(2026, 10, 1, 10, 0, tzinfo=london)) is True   # 1st again
    assert r.due_now(datetime(2026, 9, 1, 11, 0, tzinfo=london)) is False   # wrong hour


def test_month_is_honoured():
    """"0 12 20 8 *" = 20 August only, not the 20th of every month."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    london = ZoneInfo("Europe/London")
    r = _rule("0 12 20 8 *")

    assert r.due_now(datetime(2026, 8, 20, 12, 0, tzinfo=london)) is True
    assert r.due_now(datetime(2026, 9, 20, 12, 0, tzinfo=london)) is False  # Sept
    assert r.due_now(datetime(2026, 8, 21, 12, 0, tzinfo=london)) is False  # 21st


def test_dom_and_dow_together_is_refused():
    """Standard cron ORs them, which nobody expects. Refuse rather than guess."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    r = _rule("0 10 1 * 1")
    with pytest.raises(AlertConfigError, match="day-of-month and a day-of-week"):
        r.due_now(datetime(2026, 9, 1, 10, 0, tzinfo=ZoneInfo("Europe/London")))


def test_every_shipped_schedule_parses_and_fires_somewhere():
    """Every rule's schedule must be valid and actually reachable — a rule that
    can never fire looks configured but silently does nothing."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from notify.alerts import load_all
    london = ZoneInfo("Europe/London")

    start = datetime(2026, 1, 1, tzinfo=london)
    for rule in load_all():
        if not rule.schedule:
            continue
        fires = any(
            rule.due_now(start + timedelta(hours=h))
            for h in range(24 * 400)          # 400 days covers monthly + annual
        )
        assert fires, f"{rule.key}: schedule {rule.schedule!r} never fires"


# ------------------------------------------------------------- what is live

def test_only_the_morning_brief_messages_anyone():
    """Agreed 20 Aug 2026: one message a day, the morning brief, and nothing
    else until a schedule is signed off.

    This test is the guard. If someone enables a rule without that decision
    being revisited, this fails rather than five sites quietly getting an
    extra message every morning. Dated pipeline tests are exempt.
    """
    from notify.alerts import load_all

    senders = [
        r.key for r in load_all()
        if r.enabled and r.route != "none" and not r.key.startswith("pipeline-test-")
    ]
    assert senders == ["daily-site-brief"], (
        f"Expected only daily-site-brief to be live, found: {senders}. "
        f"If this is deliberate, update this test in the same commit."
    )


# ------------------------------------------------------- live sport block

from notify import sport


def test_channel_is_matched_on_fixture_id_not_team_name():
    """The whole reason channels come from FANZO rather than a TV listings
    site: the join is on a numeric id, so 'Man United' vs 'Manchester United'
    and the Man Utd / Man City collision simply cannot arise."""
    fixture = {"id": 767984, "url": "https://example.test/f"}
    page = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"extraData":{"TVGuide":{"tvGuideSSRData":{"data":['
        '{"id":767983,"name":"Arsenal vs Coventry",'
        ' "channels":[{"name":"Sky Sports Main Event"}]},'
        '{"id":767984,"name":"Hull City vs Man United",'
        ' "channels":[{"name":"TNT Sports 1"}]}'
        ']}}}}}}</script>'
    )

    class FakeResp:
        text = page
        def raise_for_status(self): pass

    real, sport.requests.get = sport.requests.get, lambda *a, **k: FakeResp()
    try:
        assert sport._channels_for(fixture) == "TNT Sports 1"
    finally:
        sport.requests.get = real


def test_fixture_with_no_channel_is_still_shown(monkeypatch):
    """A missing channel must be visible in the message, not hidden by
    dropping the fixture."""
    monkeypatch.setattr(sport, "fanzo_fixtures", lambda *a, **k: [
        {"id": 1, "time": "16:10", "name": "South Africa vs New Zealand",
         "competition": "International", "sport": "Rugby Union",
         "is_big": True, "url": None, "channel": None},
    ])
    monkeypatch.setattr(sport, "_channels_for", lambda f: None)
    out = sport.block()
    assert "South Africa vs New Zealand" in out and "16:10" in out


def test_fixtures_are_listed_in_kick_off_order(monkeypatch):
    monkeypatch.setattr(sport, "_channels_for", lambda f: "Sky Sports")
    monkeypatch.setattr(sport, "fanzo_fixtures", lambda *a, **k: sorted([
        {"id": 2, "time": "17:30", "name": "B vs C", "competition": "PL",
         "sport": "Football", "is_big": False, "url": None, "channel": None},
        {"id": 1, "time": "12:30", "name": "A vs D", "competition": "PL",
         "sport": "Football", "is_big": False, "url": None, "channel": None},
    ], key=lambda f: f["time"]))
    out = sport.block()
    assert out.index("12:30") < out.index("17:30")


def test_block_is_empty_when_nothing_is_on(monkeypatch):
    monkeypatch.setattr(sport, "fanzo_fixtures", lambda *a, **k: [])
    assert sport.block() == ""


def test_a_dead_source_costs_the_sport_block_not_the_brief(monkeypatch):
    """Both calls hit an undocumented third-party endpoint. If either dies,
    the morning brief must still go out."""
    def boom(*a, **k):
        raise RuntimeError("fanzo is down")

    monkeypatch.setattr(sport, "fanzo_fixtures", boom)
    assert sport.block() == ""                     # no exception escapes

    # fixtures fine, channel lookup dead -> times still sent
    monkeypatch.setattr(sport, "fanzo_fixtures", lambda *a, **k: [
        {"id": 1, "time": "20:00", "name": "Arsenal vs Coventry",
         "competition": "Premier League", "sport": "Football",
         "is_big": True, "url": "https://example.test/f", "channel": None}])
    monkeypatch.setattr(sport, "requests", type("R", (), {"get": staticmethod(boom)}))
    out = sport.block()
    assert "Arsenal vs Coventry" in out


def test_no_sound_note_in_the_message(monkeypatch):
    """Dropped at Ajay's request, 20 Aug 2026 — sites manage sound themselves."""
    monkeypatch.setattr(sport, "_channels_for", lambda f: "Sky Sports F1")
    monkeypatch.setattr(sport, "fanzo_fixtures", lambda *a, **k: [
        {"id": 1, "time": "15:30", "name": "Dutch Grand Prix", "competition":
         "F1", "sport": "Motor Sports", "is_big": False, "url": None,
         "channel": None}])
    assert "sound" not in sport.block().lower()


def test_only_the_brief_carries_the_sport_block():
    """The fixture list belongs on the one message a day, not bolted onto
    every future alert."""
    from notify.alerts import load_all
    carriers = [r.key for r in load_all() if r.sport_block]
    assert carriers == ["daily-site-brief"], carriers
