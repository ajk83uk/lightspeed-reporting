"""
Today's live sport for the morning brief: what we're showing, and on what.

One source, two calls:

  1. https://www-service.fanzo.com/venues/{venue}/fixture/widget-json
     The venue's OWN chosen fixtures — the same feed behind the Live Sports
     tab on tapandtandoor.co.uk. Only what someone has actually added in the
     FANZO dashboard appears here, which is exactly what we want: we don't
     show every game.

  2. https://www.fanzo.com/en/bars-showing/{fixtureId}/...   (per fixture)
     FANZO's own TV guide page, which carries a `channels` array.

Why both come from FANZO
------------------------
The obvious alternative for channels is a TV listings site, but that means
matching fixtures across two vendors by team name — and the names disagree
("Bradford" vs "Bradford City", "Man United" vs "Manchester United"). Any
suffix-stripping that makes those match also collapses "Manchester United"
and "Manchester City" onto the same key, so on a weekend when both play you
can hand a site the wrong channel with nothing to flag it.

Joining on FANZO's numeric fixture id removes that whole class of bug, and
covers rugby, F1 and cricket as well — a football-only listings site does not.

Caveats
-------
* Neither endpoint is documented, so both may change without notice. Every
  entry point here fails soft: a dead source costs the sport block, never
  the brief.
* One request per fixture. That is a handful a day, not a scrape.
* A fixture with no channel listed is still SHOWN, without one. Dropping it
  would hide the gap; printing it makes it visible.
* All five sites share venue 17079, confirmed with Ajay 20 Aug 2026, so the
  same list goes to every site.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

LONDON = ZoneInfo("Europe/London")
FANZO_VENUE = 17079
EARLIEST_HOUR = 11        # sites open at midday; don't flag a 10:30 start
FIXTURES_URL = "https://www-service.fanzo.com/venues/{venue}/fixture/widget-json"
UA = {"User-Agent": "Tap-and-Tandoor-ops/1.0"}
TIMEOUT = 20

# --- week-ahead fixture guide (for the Wednesday rota prompt) ---------------
#
# The venue feed only reaches ~4 days out, but a rota is built for the week
# AFTER next. So the week-ahead list comes from FANZO's public TV guide pages
# instead, which run several weeks ahead and carry channels.
#
# These are national listings, NOT our venue's chosen fixtures — they say
# what is ON, so managers can staff for it. Whether we show a given game is
# still a FANZO dashboard decision.
TV_GUIDE = {
    "Premier League":   "https://www.fanzo.com/en/tvguide/football/premier-league/5159",
    "Champions League": "https://www.fanzo.com/en/tvguide/football/uefa-champions-league/5205",
    "Rugby":            "https://www.fanzo.com/en/tvguide/rugby-union/5283",
}

# Rugby carries a lot of French Top 14 on Premier Sports, which draws little
# trade here. "Key" means internationals and the English club game.
KEY_RUGBY = ("international", "prem rugby", "premiership rugby", "champions cup",
             "six nations", "rugby championship", "nations championship",
             "autumn nations")

_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def fanzo_fixtures(venue: int = FANZO_VENUE, on: datetime | None = None) -> list[dict]:
    """Fixtures this venue is showing on `on` (default: today, UK time).

    Times come from `startTimeUtc`, converted here. Do NOT use the sibling
    `startTime` field: FANZO renders that in the *requester's* timezone, so it
    reads correctly from a UK machine and wrongly from a US-hosted one. That
    shipped on 21 Aug 2026 — Railway runs in the US, and the brief told five
    sites Arsenal v Coventry was at 15:00 when it was at 20:00.

    Anything kicking off before EARLIEST_HOUR is dropped: the sites open at
    midday, so a 10:30 start is not theirs to put on.
    """
    now = on or datetime.now(LONDON)
    if now.tzinfo is None:
        now = now.replace(tzinfo=LONDON)
    day = now.astimezone(LONDON).date()

    r = requests.get(FIXTURES_URL.format(venue=venue), timeout=TIMEOUT, headers=UA)
    r.raise_for_status()

    out = []
    for f in (r.json() or {}).get("result", []):
        raw = f.get("startTimeUtc")
        if not raw:
            continue
        try:
            local = datetime.fromisoformat(
                raw.replace("Z", "+00:00")).astimezone(LONDON)
        except ValueError:
            continue
        if local.date() != day or local.hour < EARLIEST_HOUR:
            continue
        out.append({
            "id": f.get("id"),
            "time": local.strftime("%H:%M"),
            "name": f.get("name", ""),
            "competition": (f.get("competition") or {}).get("name", ""),
            "sport": (f.get("sport") or {}).get("name", ""),
            "is_big": bool(f.get("isBig")),
            "url": f.get("matchpintUrl"),
            "channel": None,
        })
    return sorted(out, key=lambda f: f["time"])


def _channels_for(fixture: dict) -> str | None:
    """Channel names for one fixture, from its own FANZO page. None if absent.

    Matched on the numeric fixture id, so there is no chance of picking up a
    different match's channel.
    """
    url = fixture.get("url")
    if not url:
        return None
    try:
        html = requests.get(url, timeout=TIMEOUT, headers=UA).text
        m = _NEXT_DATA.search(html)
        if not m:
            return None
        data = json.loads(m.group(1))
    except Exception:
        return None

    found: list = []

    def walk(node):
        if found or not isinstance(node, (dict, list)):
            return
        if isinstance(node, dict):
            if node.get("id") == fixture["id"] and "channels" in node:
                found.extend(node.get("channels") or [])
                return
            for value in node.values():
                walk(value)
        else:
            for value in node:
                walk(value)

    walk(data)
    names = [c.get("name") for c in found if c.get("name")]
    return ", ".join(names) if names else None


def today(venue: int = FANZO_VENUE, on: datetime | None = None) -> list[dict]:
    """Today's fixtures with channels attached where FANZO lists them."""
    fixtures = fanzo_fixtures(venue, on)
    for f in fixtures:
        f["channel"] = _channels_for(f)
    return fixtures


def block(venue: int = FANZO_VENUE, on: datetime | None = None) -> str:
    """The '*Sport today*' section, or '' when nothing is on.

    Never raises. A dead source must cost the sport block, not the brief.
    """
    try:
        fixtures = today(venue, on)
    except Exception:
        return ""
    if not fixtures:
        return ""

    lines = ["*Sport today*"]
    for f in fixtures:
        star = " ⭐" if f["is_big"] else ""
        lines.append(f"{f['time']}  {f['name']}{star}")
        detail = f["competition"] or f["sport"]
        if f["channel"]:
            detail = f"{detail} · {f['channel']}" if detail else f["channel"]
        if detail:
            lines.append(f"        {detail}")
    return "\n".join(lines)


def _guide_fixtures(url: str) -> list[dict]:
    """Fixtures + channels from one FANZO TV guide page (server-rendered)."""
    html = requests.get(url, timeout=TIMEOUT, headers=UA).text
    m = _NEXT_DATA.search(html)
    if not m:
        return []
    data = json.loads(m.group(1))
    return (data["props"]["pageProps"]["extraData"]["TVGuide"]
                ["tvGuideSSRData"]["data"])


def week_ahead(on: datetime | None = None) -> tuple[dict, bool]:
    """Key fixtures for the week a rota is being built for.

    Returns ({date: [fixture, ...]}, truncated) where `truncated` names any
    competition whose listing hit FANZO's 10-fixture page cap before the week
    ended. Better to say the list is partial than to let a manager staff a
    Tuesday believing they've seen the whole card.
    """
    now = on or datetime.now(LONDON)
    monday = (now + timedelta(days=7 - now.weekday())).date()
    sunday = monday + timedelta(days=6)

    days: dict = {}
    truncated: list = []

    for label, url in TV_GUIDE.items():
        try:
            raw = _guide_fixtures(url)
        except Exception:
            truncated.append(label)
            continue
        if not raw:
            continue

        seen_to = None
        for f in raw:
            try:
                local = datetime.fromisoformat(
                    f["startTimeUtc"].replace("Z", "+00:00")).astimezone(LONDON)
            except (KeyError, ValueError):
                continue
            seen_to = local.date() if seen_to is None else max(seen_to, local.date())

            comp = (f.get("competition") or {}).get("name", "")
            if label == "Rugby" and not any(k in comp.lower() for k in KEY_RUGBY):
                continue
            if not (monday <= local.date() <= sunday):
                continue

            days.setdefault(local.date(), []).append({
                "time": local.strftime("%H:%M"),
                "name": f.get("name", ""),
                "competition": comp or label,
                "channel": ", ".join(c["name"] for c in (f.get("channels") or [])
                                     if c.get("name")) or None,
            })

        # FANZO caps each guide page at 10 fixtures with no pagination (tested
        # 4 Sep 2026: page/limit/date params all ignored). If we got the full
        # 10 AND they stop before the week ends, there are more we can't see —
        # a Champions League matchday is ~18 games, so this fires on those.
        if len(raw) >= 10 and seen_to is not None and seen_to < sunday:
            truncated.append(label)

    for d in days:
        days[d].sort(key=lambda x: x["time"])
    return days, truncated


def week_ahead_block(on: datetime | None = None) -> str:
    """Day-by-day fixture overview for the rota week. '' if nothing found.

    Never raises — a dead source must not stop the rota prompt going out.
    """
    try:
        days, truncated = week_ahead(on)
    except Exception:
        return ""
    if not days:
        return ""

    monday = min(days)
    lines = [f"*Sport in the week you're planning* ({monday:%-d %b} onwards)"]
    for day in sorted(days):
        lines.append("")
        lines.append(f"*{day:%A %-d %b}*")
        for f in days[day]:
            lines.append(f"  {f['time']}  {f['name']}")
            detail = f["competition"]
            if f["channel"]:
                detail += f" · {f['channel']}"
            lines.append(f"          {detail}")
    if truncated:
        lines.append("")
        lines.append(f"_{' and '.join(truncated)}: the listing caps at 10 games, "
                     f"so the biggest are shown but not the full card._")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if "--week" in sys.argv:
        print(week_ahead_block() or "(no key fixtures found)")
    else:
        print(block() or "(nothing on today)")
