from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from moneymaker.models import Market

TOLERANCE = Decimal("0.011")

_NUM = r"\$?\d[\d,]*(?:\.\d+)?\s*[kKmM%]?"
_RANGE = re.compile(rf"(?:between\s+)?({_NUM})\s*(?:to|and|-|\u2013)\s*({_NUM})", re.I)
_UP_AFTER = re.compile(rf"({_NUM})\s*(?:\+|or above|or more|or higher|and above|or greater)", re.I)
_DOWN_AFTER = re.compile(rf"({_NUM})\s*(?:or below|or less|or lower|and below)", re.I)
_UP_BEFORE = re.compile(
    r"(?:above|over|more than|greater than|at least|exceeds?|higher than|reach(?:es)?|hit)"
    rf"\s+({_NUM})",
    re.I,
)
_DOWN_BEFORE = re.compile(rf"(?:below|under|less than|at most|lower than|beneath)\s+({_NUM})", re.I)
_TIME = re.compile(r"\b\d{1,2}:\d{2}\s*(?:am|pm)?\b", re.I)
_MONTH_DAY = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\b",
    re.I,
)
_VS = re.compile(r"\s+(?:vs\.?|v\.?|versus)\s+", re.I)


@dataclass(frozen=True)
class Spec:
    """The strike structure and participants parsed from a market's title and YES label."""

    kind: str
    lo: Decimal | None = None
    hi: Decimal | None = None
    direction: str | None = None
    numbers: frozenset[Decimal] = frozenset()
    parties: frozenset[str] = frozenset()
    text: str = ""


@dataclass(frozen=True)
class Check:
    """Outcome of comparing two Specs: ok, implication, or reject, with a reason."""

    result: str
    reason: str = ""
    broader: str | None = None


def parse_number(token: str) -> Decimal | None:
    """Parses tokens like $150k, 3.75%, 1,000 or 45 into a Decimal, or None if unparsable."""
    t = token.strip().lower().replace("$", "").replace(",", "").replace(" ", "")
    mult = Decimal(1)
    if t.endswith("%"):
        t = t[:-1]
    elif t.endswith("k"):
        t, mult = t[:-1], Decimal(1000)
    elif t.endswith("m"):
        t, mult = t[:-1], Decimal(1_000_000)
    try:
        return Decimal(t) * mult
    except InvalidOperation:
        return None


def _is_year(token: str) -> bool:
    """Returns True for bare four-digit years between 1900 and 2100."""
    t = token.strip()
    return t.isdigit() and len(t) == 4 and 1900 <= int(t) <= 2100


def _clean(text: str) -> str:
    """Removes clock times and month-day dates so they are not read as strikes."""
    return _MONTH_DAY.sub(" ", _TIME.sub(" ", text))


def _parties(title: str) -> frozenset[str]:
    """Returns lowercase surnames for an 'X vs Y' title, or an empty set."""
    parts = _VS.split(title)
    if len(parts) != 2:
        return frozenset()
    names = []
    for part in parts:
        words = re.findall(r"[A-Za-zÀ-ÿ'\-]+", part)
        if words:
            names.append(words[-1].lower())
    return frozenset(names) if len(names) == 2 else frozenset()


def describe(market: Market) -> Spec:
    """Parses a market's title and YES label into a range, threshold, or plain Spec."""
    raw = f"{market.title} {market.yes_label or ''}"
    text = _clean(raw)
    parties = _parties(market.title.split(":")[0]) or _parties(market.yes_label or "")
    numbers = frozenset(
        n
        for tok in re.findall(_NUM, text)
        if not _is_year(tok) and (n := parse_number(tok)) is not None
    )

    focus = _clean(market.yes_label or "") or text
    for source in (focus, text):
        m = _RANGE.search(source)
        if m and not (_is_year(m.group(1)) or _is_year(m.group(2))):
            lo, hi = parse_number(m.group(1)), parse_number(m.group(2))
            if lo is not None and hi is not None and lo < hi:
                return Spec("range", lo=lo, hi=hi, numbers=numbers, parties=parties, text=raw)
        for pattern, direction in (
            (_UP_AFTER, "up"),
            (_DOWN_AFTER, "down"),
            (_UP_BEFORE, "up"),
            (_DOWN_BEFORE, "down"),
        ):
            m = pattern.search(source)
            if m and not _is_year(m.group(1)):
                value = parse_number(m.group(1))
                if value is not None:
                    return Spec(
                        "threshold", lo=value, direction=direction, numbers=numbers,
                        parties=parties, text=raw,
                    )
    return Spec("plain", numbers=numbers, parties=parties, text=raw)


_TRIMMED = re.compile(r"trimmed mean", re.I)
_SIMPLE = re.compile(r"simple average|the average of these prices", re.I)


def settlement_method(rules: str) -> str | None:
    """Returns 'trimmed' or 'simple' when the rules name how the settlement price is averaged."""
    if _TRIMMED.search(rules):
        return "trimmed"
    if _SIMPLE.search(rules):
        return "simple"
    return None


def compare_methods(kalshi: Market, pm: Market) -> Check:
    """Rejects pairs whose rules average the settlement price differently (simple vs trimmed)."""
    a, b = settlement_method(kalshi.rules), settlement_method(pm.rules)
    if a and b and a != b:
        return Check("reject", "different averaging method")
    return Check("ok")


def _close(a: Decimal | None, b: Decimal | None) -> bool:
    """Returns True when two strikes are equal within a cent or one part per million."""
    if a is None or b is None:
        return False
    return abs(a - b) <= max(TOLERANCE, abs(a) * Decimal("0.000001"))


def compare(kalshi: Spec, pm: Spec) -> Check:
    """Compares strike structure and participants of a Kalshi and a PM market.

    Returns reject for structures that can never settle together, implication for
    same-direction thresholds at different strikes, and ok otherwise.
    """
    for a, b in ((kalshi, pm), (pm, kalshi)):
        if a.parties and not any(name in b.text.lower() for name in a.parties):
            return Check("reject", "different participants")

    kinds = {kalshi.kind, pm.kind}
    if kinds == {"range"}:
        if _close(kalshi.lo, pm.lo) and _close(kalshi.hi, pm.hi):
            return Check("ok")
        return Check("reject", "different ranges")
    if kinds == {"range", "threshold"}:
        return Check("reject", "range vs threshold")
    if kinds == {"threshold"}:
        if kalshi.direction == pm.direction:
            if _close(kalshi.lo, pm.lo):
                return Check("ok")
            lower_is_kalshi = kalshi.lo < pm.lo  # type: ignore[operator]
            kalshi_broader = lower_is_kalshi if kalshi.direction == "up" else not lower_is_kalshi
            return Check(
                "implication",
                "same-direction thresholds at different strikes",
                broader="kalshi" if kalshi_broader else "pm",
            )
        if _close(kalshi.lo, pm.lo):
            return Check("ok", "opposite directions at the same strike")
        return Check("reject", "opposite thresholds at different strikes")
    if kalshi.numbers and pm.numbers and not any(
        _close(a, b) for a in kalshi.numbers for b in pm.numbers
    ):
        return Check("reject", "different numbers")
    return Check("ok")
