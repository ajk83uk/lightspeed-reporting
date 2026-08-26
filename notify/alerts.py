"""
Config-driven alert engine: Metabase rows in, ZenZap messages out.

Rules live in config/alerts.yaml. This module holds no business logic —
adding an alert should never require editing Python.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

ALERTS_PATH = Path(__file__).resolve().parent / "config" / "alerts.yaml"
REMINDERS_PATH = Path(__file__).resolve().parent / "config" / "reminders.yaml"

# The Railway service wakes on these minutes past the hour
# (railway.brief.json: "*/15 * * * *"). A rule scheduled for any other minute
# would simply never fire, so it is rejected at load rather than going quiet.
SLOT_MINUTES = {0, 15, 30, 45}
SLOT_LENGTH = 15


def _slot(minute: int) -> int:
    """Which 15-minute slot a clock minute belongs to.

    Railway "does not guarantee execution times to the minute as they can vary
    by a few minutes" (their docs), and the container takes time to start on
    top of that. So a job triggered for 11:00 commonly runs at 11:01-11:03.

    Matching the minute EXACTLY meant such a run found nothing due and exited
    reporting success — which is what silently killed the 11:00 brief on
    26 Aug 2026. Comparing slots instead tolerates the drift while still
    letting a rule ask for :15, :30 or :45.
    """
    return (minute // SLOT_LENGTH) * SLOT_LENGTH

OPS = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}


class AlertConfigError(ValueError):
    """Raised when a rule in alerts.yaml is malformed."""


@dataclass
class Rule:
    key: str
    title: str
    enabled: bool
    # Reminders carry no SQL — they fire on a schedule and say something.
    # Alerts carry SQL and only fire when the data says so.
    sql: Optional[str]
    condition: dict
    message: str
    route: str
    database: int
    site_column: str
    schedule: Optional[str] = None
    description: Optional[str] = None
    sites: Optional[list] = None      # reminders: limit to these site keys
    sport_block: bool = False         # append today's live-sport fixtures
    weekend_shift: Optional[str] = None   # 'previous_friday' — see due_now

    @property
    def is_reminder(self) -> bool:
        """No query — nothing to evaluate, just a scheduled message."""
        return not self.sql

    def due_now(self, now) -> bool:
        """True if this rule's `schedule:` falls in the current local hour.

        One Railway service runs hourly and asks every rule this question, so
        adding a message at a new time is a config change, not a new service.

        Fields: minute hour day-of-month month day-of-week

        The MINUTE is ignored — the service fires once an hour, so the hour is
        the finest granularity available. Every other field is honoured:

            "0 11 * * *"     every day at 11:00
            "0 9 * * 1"      Mondays at 09:00
            "0 9 * * 1-5"    weekdays at 09:00
            "0 10 1 * *"     1st of the month at 10:00
            "0 12 20 8 *"    20 August at 12:00

        A rule with no schedule never fires automatically; it can still be run
        by hand with --rule. Times are Europe/London, so this follows the
        clocks by itself.
        """
        if not self.schedule:
            return False

        parts = self.schedule.split()
        if len(parts) != 5:
            raise AlertConfigError(
                f"Rule '{self.key}': schedule {self.schedule!r} must have 5 "
                f"cron fields, e.g. '0 11 * * *'"
            )
        minute, hour, dom, month, dow = parts

        # Until 22 Aug 2026 the service ran hourly and the minute was ignored.
        # It now wakes every 15 min so a rule can ask for 10:45.
        for token in minute.replace("-", ",").split(","):
            if token != "*" and int(token) not in SLOT_MINUTES:
                raise AlertConfigError(
                    f"Rule '{self.key}': schedule {self.schedule!r} wants minute "
                    f"{token}, but the service only wakes at "
                    f"{sorted(SLOT_MINUTES)} past the hour. It would never fire."
                )

        def field_matches(field: str, value: int, wrap: int | None = None) -> bool:
            """True if `value` satisfies a cron field (*, n, a-b, or a list)."""
            if field == "*":
                return True
            allowed: set[int] = set()
            for token in field.split(","):
                if "-" in token:
                    lo, hi = (int(x) for x in token.split("-"))
                    if wrap is not None:
                        lo, hi = lo % wrap, hi % wrap
                    allowed |= set(range(lo, hi + 1))
                else:
                    n = int(token)
                    allowed.add(n % wrap if wrap is not None else n)
            return value in allowed

        # Standard cron ORs day-of-month against day-of-week when BOTH are
        # restricted, which almost nobody expects. Rather than silently guess,
        # refuse — no real reminder needs "the 1st AND Mondays".
        if dom != "*" and dow != "*":
            raise AlertConfigError(
                f"Rule '{self.key}': schedule {self.schedule!r} sets both a "
                f"day-of-month and a day-of-week. Standard cron treats that as "
                f"OR, which is rarely what is meant. Use one or the other."
            )

        def minute_matches() -> bool:
            if minute == "*":
                return True
            return any(_slot(int(tok)) == _slot(now.minute)
                       for tok in minute.replace("-", ",").split(","))

        if self.weekend_shift:
            return minute_matches() and self._due_with_weekend_shift(
                now, hour, dom, month)

        if not minute_matches():
            return False
        if not field_matches(hour, now.hour):
            return False
        if not field_matches(month, now.month):
            return False
        if not field_matches(dom, now.day):
            return False
        # cron: 0 and 7 both mean Sunday; Python: Monday=0..Sunday=6
        if not field_matches(dow, (now.weekday() + 1) % 7, wrap=7):
            return False

        return True

    def _due_with_weekend_shift(self, now, hour, dom, month) -> bool:
        """Monthly rules that must not land on a weekend.

        `weekend_shift: previous_friday` means: fire on the configured day of
        the month, unless that day is a Saturday or Sunday, in which case fire
        on the Friday before it instead.

        Cron cannot express this — "the 25th" and "a weekday" are separate
        fields that cron ORs rather than ANDs. So the schedule keeps saying
        25 (which is what the reminder is *about*, and what reads correctly in
        the config) and this shifts the actual send.

        Worth it because a payroll cut-off prompt is useless landing mid-shift
        on a Saturday — 25 Oct 2026 is a Sunday, 25 Apr 2026 a Saturday.
        """
        from calendar import monthrange
        from datetime import date, timedelta

        if hour != "*" and now.hour != int(hour.split(",")[0]):
            return False
        if month != "*" and now.month not in {int(x) for x in month.split(",")}:
            return False
        try:
            target_day = int(dom)
        except ValueError:
            raise AlertConfigError(
                f"Rule '{self.key}': weekend_shift needs a single day-of-month "
                f"in its schedule, got {dom!r}"
            )

        last = monthrange(now.year, now.month)[1]
        target = date(now.year, now.month, min(target_day, last))

        # Mon=0 .. Sat=5, Sun=6
        if target.weekday() == 5:
            target -= timedelta(days=1)          # Saturday -> Friday
        elif target.weekday() == 6:
            target -= timedelta(days=2)          # Sunday   -> Friday

        return now.date() == target

    @property
    def is_brief(self) -> bool:
        return self.condition == "always" or bool(
            isinstance(self.condition, dict) and self.condition.get("always"))

    def matches(self, row: dict) -> bool:
        """True if the row breaches every clause in `condition.all`.

        A row missing a referenced column, or holding None there, never
        matches. Treating absent data as a breach would alert on broken
        pipelines; treating it as 0 would go blind. Neither is acceptable,
        so it's skipped and surfaced separately by a data-quality rule.
        """
        if self.condition == "always" or self.condition.get("always"):
            return True   # briefs send every day regardless of thresholds

        clauses = self.condition.get("all")
        if not clauses:
            raise AlertConfigError(f"Rule '{self.key}' has no condition.all clauses")

        for clause in clauses:
            column, op_name = clause["column"], clause["op"]
            if op_name not in OPS:
                raise AlertConfigError(f"Rule '{self.key}': unknown operator {op_name!r}")
            actual = row.get(column)
            if actual is None:
                return False
            try:
                if not OPS[op_name](float(actual), float(clause["value"])):
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def render(self, row: dict, site_display: str) -> str:
        """Fill the message template from the row.

        Supports `{column}` and `{column:date}` (renders 2026-08-18 as
        'Tue 18 Aug'). Missing keys raise rather than printing 'None' into a
        staff group.
        """
        values: dict[str, Any] = {"site_display": site_display}
        for key, value in row.items():
            values[key] = "" if value is None else value
            values[f"{key}:date"] = _pretty_date(value)

        text = self.message
        for key in sorted(values, key=len, reverse=True):
            text = text.replace("{" + key + "}", str(values[key]))

        if "{" in text and "}" in text:
            leftover = text[text.index("{"): text.index("}") + 1]
            raise AlertConfigError(
                f"Rule '{self.key}': message references {leftover} which the query "
                f"doesn't return. Available: {sorted(row)}"
            )
        return text.strip()


def _pretty_date(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.replace("Z", "+0000"), fmt).strftime("%a %d %b")
        except ValueError:
            continue
    return text


def load_rules(path: Path | str = ALERTS_PATH) -> list[Rule]:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    defaults = raw.get("defaults", {})
    rules = []

    for entry in raw.get("rules", []):
        missing = {"key", "condition", "message", "route"} - set(entry)
        if missing:
            raise AlertConfigError(
                f"Rule {entry.get('key', '<unnamed>')!r} missing: {sorted(missing)}"
            )
        rules.append(Rule(
            key=entry["key"],
            title=entry.get("title", entry["key"]),
            enabled=entry.get("enabled", False),
            sql=entry.get("sql"),
            condition=entry["condition"],
            message=entry["message"],
            route=entry["route"],
            database=entry.get("database", defaults.get("database", 2)),
            site_column=entry.get("site_column", defaults.get("site_column", "site_name")),
            schedule=entry.get("schedule"),
            description=entry.get("description"),
            sites=entry.get("sites"),
            sport_block=entry.get("sport_block", False),
            weekend_shift=entry.get("weekend_shift"),
        ))

    keys = [r.key for r in rules]
    if len(keys) != len(set(keys)):
        raise AlertConfigError("Duplicate rule keys in alerts.yaml")
    return rules


def max_sites_per_run(path: Path | str = ALERTS_PATH) -> int:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return raw.get("defaults", {}).get("max_sites_per_run", 5)


def load_reminders(path: Path | str = REMINDERS_PATH) -> list[Rule]:
    """Scheduled messages that query nothing.

    Kept in a separate file from alerts.yaml on purpose: an alert answers
    "did something happen?" and a reminder answers "is it that time again?".
    Mixing them makes both harder to scan.
    """
    p = Path(path)
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text()) or {}
    out = []

    for entry in raw.get("reminders", []):
        missing = {"key", "message", "route"} - set(entry)
        if missing:
            raise AlertConfigError(
                f"Reminder {entry.get('key', '<unnamed>')!r} missing: {sorted(missing)}"
            )
        out.append(Rule(
            key=entry["key"],
            title=entry.get("title", entry["key"]),
            enabled=entry.get("enabled", False),
            sql=None,                       # <- what makes it a reminder
            condition="always",             # a reminder always fires when due
            message=entry["message"],
            route=entry["route"],
            database=0,
            site_column="",
            schedule=entry.get("schedule"),
            description=entry.get("description"),
            sites=entry.get("sites"),
            weekend_shift=entry.get("weekend_shift"),
        ))

    keys = [r.key for r in out]
    if len(keys) != len(set(keys)):
        raise AlertConfigError("Duplicate reminder keys in reminders.yaml")
    return out


def load_all() -> list[Rule]:
    """Alerts and reminders together. Keys must be unique across both, since
    the key is the idempotency handle on every message sent."""
    rules = load_rules() + load_reminders()
    keys = [r.key for r in rules]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        raise AlertConfigError(
            f"Key(s) {sorted(dupes)} used in both alerts.yaml and reminders.yaml"
        )
    return rules
