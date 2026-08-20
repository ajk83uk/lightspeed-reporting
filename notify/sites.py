"""Site registry loader — resolves config/sites.yaml and merges defaults."""

from __future__ import annotations

import copy
import os
from datetime import time as dtime
from pathlib import Path
from typing import Any, Optional

import yaml

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "sites.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Site(dict):
    """A site with defaults already merged in. Behaves like a dict."""

    @property
    def key(self) -> str:
        return self["key"]

    @property
    def name(self) -> str:
        return self["name"]

    @property
    def uses_zenzap(self) -> bool:
        """False for sites not on ZenZap (currently Zindiya).

        Jobs must check this before attempting to notify. A site with no
        messaging platform isn't an error — it just doesn't get notified.
        """
        return bool(self.get("zenzap_topic"))

    @property
    def topic(self) -> str:
        """externalId of the site's own ZenZap chat."""
        if not self.uses_zenzap:
            raise KeyError(f"Site '{self.key}' is not on ZenZap")
        return self["zenzap_topic"]

    @property
    def region(self) -> str:
        return self.get("region", "")


class SiteRegistry:
    def __init__(self, path: Path | str = CONFIG_PATH):
        raw = yaml.safe_load(Path(path).read_text())
        self.defaults: dict[str, Any] = raw.get("defaults", {})
        self.groups: dict[str, dict] = raw.get("groups") or {}
        self._sites: list[Site] = []
        for entry in raw.get("sites", []):
            overrides = entry.pop("overrides", {})
            merged = _deep_merge(self.defaults, overrides)
            merged.update(entry)
            self._sites.append(Site(merged))

    def group_topic(self, name: str) -> Optional[str]:
        """externalId of a shared group ('south_coast', 'announcements')."""
        group = self.groups.get(name)
        return group.get("zenzap_topic") if group else None

    def sites_in_group(self, group_name: str) -> list["Site"]:
        """Sites a broadcast group covers. Empty list for 'announcements'
        (all sites) is deliberate — callers should use all() for that."""
        group = self.groups.get(group_name) or {}
        keys = set(group.get("covers") or [])
        return [s for s in self._sites if s["key"] in keys]

    def all_topic_names(self) -> set[str]:
        """Every ZenZap group name this config expects to exist."""
        names = {s.topic for s in self._sites if s.uses_zenzap}
        names |= {g["zenzap_topic"] for g in self.groups.values() if g.get("zenzap_topic")}
        return names

    def __iter__(self):
        return iter(self._sites)

    def __len__(self):
        return len(self._sites)

    def all(self) -> list[Site]:
        return list(self._sites)

    def on_zenzap(self) -> list[Site]:
        """Only sites that can actually receive ZenZap notifications."""
        return [s for s in self._sites if s.uses_zenzap]

    def by_key(self, key: str) -> Site:
        for site in self._sites:
            if site["key"] == key:
                return site
        raise KeyError(f"Unknown site key: {key}")

    def by_location_id(self, location_id) -> Optional[Site]:
        """Look up by Lightspeed business_location_id.

        Preferred over name matching: site NAMES differ across sources
        ("Solihull" in Nory and bookings, "Tap Solihull" in the POS views,
        and "Tap Bournemouth." carries a trailing full stop). The numeric
        id is the only handle that is the same everywhere.
        """
        target = str(location_id).strip()
        for site in self._sites:
            if str(site.get("lightspeed_location_id", "")).strip() == target:
                return site
        return None

    def by_nory_name(self, name: str) -> Optional[Site]:
        target = (name or "").strip().casefold()
        for site in self._sites:
            if site.get("nory_location_name", "").strip().casefold() == target:
                return site
        return None

    def by_favourite_table_venue(self, venue: str) -> Optional[Site]:
        """Match a venue string from a Favourite Table email.

        Tolerant matching: FT emails don't always use the exact configured name,
        so fall back to a containment check before giving up.
        """
        target = (venue or "").strip().casefold()
        if not target:
            return None
        for site in self._sites:
            if site.get("favourite_table_venue", "").strip().casefold() == target:
                return site
        for site in self._sites:
            configured = site.get("favourite_table_venue", "").strip().casefold()
            if configured and (configured in target or target in configured):
                return site
        return None

    def by_inbox(self, address: str) -> Optional[Site]:
        target = (address or "").strip().casefold()
        for site in self._sites:
            if site.get("site_inbox", "").strip().casefold() == target:
                return site
        return None


def _parse_hhmm(value: str) -> dtime:
    hours, minutes = value.split(":")
    return dtime(int(hours), int(minutes))


def in_quiet_hours(site: Site, now) -> bool:
    """True if `now` (a tz-aware or naive datetime) falls inside quiet hours.

    Handles windows that wrap past midnight (e.g. 23:00 -> 08:00).
    """
    window = site.get("quiet_hours")
    if not window:
        return False
    start = _parse_hhmm(window["start"])
    end = _parse_hhmm(window["end"])
    current = now.time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def registry(path: Path | str | None = None) -> SiteRegistry:
    return SiteRegistry(path or os.environ.get("SITES_CONFIG", CONFIG_PATH))
