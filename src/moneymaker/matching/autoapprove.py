from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from moneymaker.matching.extract import Check, settlement_method
from moneymaker.matching.routing import Thresholds
from moneymaker.matching.store import MatchStore, Routed
from moneymaker.models import Market, Venue

NEW_YORK = ZoneInfo("America/New_York")
AUTO_STATUSES = ("uncertain", "pending_review")

_ASSETS = r"bitcoin|btc|ethereum|ether|eth|solana|sol|xrp|ripple|dogecoin|doge"
_MONTHS = (
    r"january|february|march|april|may|june|july|august|september|october|november|december"
    r"|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec"
)
_WEEKDAYS = (
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|mon|tues|tue|wed|thurs|thu|fri|sat|sun"
)

_URL = re.compile(r"https?://\S+|www\.\S+")
_INDEX_CODE = re.compile(r"\b(?:[a-z]{3,4}usd_?rti|[be]rti|rti)\b")
_ASSET = re.compile(rf"\b(?:{_ASSETS})\b")
_INDEX_NAME = re.compile(
    r"<asset>(?:-dollar)? real[- ]time index(?: \(<index>\))?|<asset> \(<index>\)"
)
_MONTH = re.compile(rf"\b(?:{_MONTHS.replace('may|', '')}|may(?=\s+\d))\b\.?")
_WEEKDAY = re.compile(rf"\b(?:{_WEEKDAYS})\b\.?")
_TZ = re.compile(r"\b(?:et|est|edt)\b")
_CLOCK = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b")
_NUMBER = re.compile(r"\$?\d+(?:,\d{3})*(?:\.\d+)?")
_SPACE = re.compile(r"\s+")
_ASSET_WORD = re.compile(rf"\b(?:{_ASSETS})\b|\b([a-z]{{3,4}})usd_?rti\b|\b([be])rti\b")
_CANONICAL = {
    "bitcoin": "btc", "b": "btc", "ethereum": "eth", "ether": "eth", "e": "eth",
    "solana": "sol", "ripple": "xrp", "dogecoin": "doge",
}

_MONTH_NUM = {
    name: i
    for i, names in enumerate(
        (
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}


def _time_re(p: str) -> str:
    """Returns a regex for a clock time with am/pm and a timezone, with groups prefixed by p."""
    return (
        rf"\b(?P<{p}h>\d{{1,2}})(?::(?P<{p}m>\d{{2}}))?\s*(?P<{p}ap>am|pm)"
        rf"\s*(?P<{p}tz>et|est|edt|utc)\b"
    )


def _date_re(p: str) -> str:
    """Returns a regex for 'Fri, Nov 6, 2026'-style dates, with groups prefixed by p."""
    return (
        rf"(?:\b(?:{_WEEKDAYS})\.?,?\s+)?\b(?P<{p}mon>{_MONTHS})\.?\s+(?P<{p}day>\d{{1,2}})"
        rf"(?:st|nd|rd|th)?,?\s+(?P<{p}year>\d{{4}})"
    )


_TIME_THEN_DATE = re.compile(_time_re("t") + r",?\s+(?:on\s+)?" + _date_re("d"), re.I)
_DATE_THEN_TIME = re.compile(_date_re("d") + r",?\s+(?:at\s+)?" + _time_re("t"), re.I)


def rules_fingerprint(text: str) -> str:
    """Returns the rules text with URLs, assets, index names, dates, timezones and numbers masked.

    Two markets from the same template, differing only in asset, strike or date, share a
    fingerprint.
    """
    s = text.lower()
    s = _URL.sub("<url>", s)
    s = _INDEX_CODE.sub("<index>", s)
    s = _ASSET.sub("<asset>", s)
    s = _INDEX_NAME.sub("<index>", s)
    s = _MONTH.sub("<month>", s)
    s = _WEEKDAY.sub("<weekday>", s)
    s = _TZ.sub("<tz>", s)
    s = _CLOCK.sub("#", s)
    s = _NUMBER.sub("#", s)
    return _SPACE.sub(" ", s).strip()


def rules_assets(text: str) -> frozenset[str]:
    """Returns the canonical asset codes named in rules text, from asset names and index codes."""
    out = set()
    for m in _ASSET_WORD.finditer(text.lower()):
        token = m.group(1) or m.group(2) or m.group(0)
        out.add(_CANONICAL.get(token, token))
    return frozenset(out)


def _instant(m: re.Match[str]) -> datetime | None:
    """Converts one time-and-date regex match into a UTC datetime, or None if invalid."""
    hour, minute = int(m["th"]), int(m["tm"] or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    hour = hour % 12 + (12 if m["tap"].lower() == "pm" else 0)
    tz = UTC if m["ttz"].lower() == "utc" else NEW_YORK
    try:
        local = datetime(
            int(m["dyear"]), _MONTH_NUM[m["dmon"].lower()], int(m["dday"]), hour, minute,
            tzinfo=tz,
        )
    except ValueError:
        return None
    return local.astimezone(UTC)


def settlement_instants(text: str) -> frozenset[datetime]:
    """Returns every time-plus-date mention in rules text as a UTC datetime.

    Both 'time on date' and 'date at time' orders are parsed; ET, EST and EDT are read as
    America/New_York local time.
    """
    out = set()
    for pattern in (_TIME_THEN_DATE, _DATE_THEN_TIME):
        for m in pattern.finditer(text):
            if (t := _instant(m)) is not None:
                out.add(t)
    return frozenset(out)


@dataclass
class Template:
    """What the user has decided about one (Kalshi fingerprint, PM fingerprint) pair."""

    polarity: str | None = None
    count: int = 0
    min_same_event: float | None = None
    conflicted: bool = False
    examples: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AutoDecision:
    """Outcome of an auto-approval check: approved or not, the polarity, and the reasons."""

    approved: bool
    polarity: str | None
    reasons: list[str]


class AutoApprover:
    """Approves pairs whose rules match a user-approved template and pass deterministic checks."""

    def __init__(self, templates: dict[tuple[str, str], Template], settings: dict[str, Any]):
        """Stores the template map and the [auto] threshold settings."""
        self.templates = templates
        self.enabled = bool(settings.get("enabled", False))
        self.min_examples = int(settings.get("min_examples", 1))
        self.polarity_min = float(settings.get("polarity_min", 0.90))
        self.same_event_margin = float(settings.get("same_event_margin", 0.15))
        self.same_event_floor = float(settings.get("same_event_floor", 0.25))

    @classmethod
    def from_store(cls, store: MatchStore, thresholds: Thresholds) -> AutoApprover:
        """Builds templates from the user's approved and rejected equivalents in the store.

        Rejections whose note starts with 'rule:' are bulk rule rejections and are ignored.
        """
        templates: dict[tuple[str, str], Template] = {}
        rows = store.db.execute(
            "SELECT kalshi_id, pm_id, status, polarity, scores, note FROM matches "
            "WHERE decided_by = 'user' AND status IN ('approved_equivalent', 'rejected')"
        ).fetchall()
        for row in rows:
            if row["status"] == "rejected" and (row["note"] or "").startswith("rule:"):
                continue
            kalshi = store.get_market(str(Venue.KALSHI), row["kalshi_id"])
            pm = store.get_market(str(Venue.POLYMARKET_US), row["pm_id"])
            if kalshi is None or pm is None:
                continue
            key = (rules_fingerprint(kalshi.rules), rules_fingerprint(pm.rules))
            tpl = templates.setdefault(key, Template())
            if row["status"] == "rejected":
                tpl.conflicted = True
                continue
            if tpl.polarity is not None and tpl.polarity != row["polarity"]:
                tpl.conflicted = True
            tpl.polarity = tpl.polarity or row["polarity"]
            tpl.count += 1
            tpl.examples.append(f"{row['kalshi_id']} / {row['pm_id']}")
            same_event = json.loads(row["scores"] or "{}").get("same_event")
            if same_event is not None:
                tpl.min_same_event = (
                    same_event if tpl.min_same_event is None
                    else min(tpl.min_same_event, same_event)
                )
        return cls(templates, thresholds.data.get("auto", {}))

    def check(
        self, kalshi: Market, pm: Market, shape_check: Check, routed: Routed
    ) -> AutoDecision:
        """Returns whether a pair can be auto-approved as an equivalent of a known template."""
        key = (rules_fingerprint(kalshi.rules), rules_fingerprint(pm.rules))
        tpl = self.templates.get(key)
        if not self.enabled:
            return AutoDecision(False, None, ["auto-approval disabled"])
        if tpl is None or tpl.count == 0:
            return AutoDecision(False, None, ["no approved template"])
        refuse: list[str] = []
        if tpl.conflicted:
            refuse.append("template conflicted by a user rejection or polarity")
        if tpl.count < self.min_examples:
            refuse.append(f"template has {tpl.count} < {self.min_examples} examples")
        if shape_check.result != "ok":
            refuse.append(f"shape check {shape_check.result}")
        if rules_assets(kalshi.rules) != rules_assets(pm.rules):
            refuse.append("assets differ")
        k_times, p_times = settlement_instants(kalshi.rules), settlement_instants(pm.rules)
        if not k_times or not p_times:
            refuse.append("settlement time not found in rules")
        elif k_times != p_times:
            refuse.append("settlement times differ")
        if settlement_method(kalshi.rules) != settlement_method(pm.rules):
            refuse.append("settlement methods differ")
        scores = routed.scores or {}
        same_event, polarity, polarity_p = (
            scores.get("same_event"), scores.get("polarity"), scores.get("polarity_p")
        )
        if same_event is None or polarity is None or polarity_p is None:
            refuse.append("Jev scores missing")
        else:
            floor = self.same_event_floor
            if tpl.min_same_event is not None:
                floor = max(floor, tpl.min_same_event - self.same_event_margin)
            if polarity != tpl.polarity:
                refuse.append(f"Jev polarity {polarity} != approved {tpl.polarity}")
            if polarity_p < self.polarity_min:
                refuse.append(f"polarity_p {polarity_p:.2f} < {self.polarity_min}")
            if same_event < floor:
                refuse.append(f"same_event {same_event:.2f} < {floor:.2f}")
        if refuse:
            return AutoDecision(False, None, refuse)
        stamp = ", ".join(sorted(t.strftime("%Y-%m-%d %H:%M UTC") for t in k_times))
        return AutoDecision(True, tpl.polarity, [
            f"auto-approved: template of {tpl.count} user-approved pair(s), e.g. "
            f"{tpl.examples[0]}",
            f"settlement times match ({stamp})",
        ])
