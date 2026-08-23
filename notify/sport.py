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
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

LONDON = ZoneInfo("Europe/London")
FANZO_VENUE = 17079
EARLIEST_HOUR = 11        # sites open at midday; don't flag a 10:30 start
FIXTURES_URL = "https://www-service.fanzo.com/venues/{venue}/fixture/widget-json"
UA = {"User-Agent": "Tap-and-Tandoor-ops/1.0"}
TIMEOUT = 20

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


if __name__ == "__main__":
    print(block() or "(nothing on today)")
