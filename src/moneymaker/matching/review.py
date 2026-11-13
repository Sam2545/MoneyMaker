from __future__ import annotations

import json
import re
import sqlite3
import textwrap
from collections.abc import Callable, Sequence

from moneymaker.matching.store import MatchStore
from moneymaker.models import Market, Venue

DEFAULT_STATUSES = ("pending_review", "suspended", "unclassified")
RULES_PREVIEW = 600
WIDTH = 100

InputFn = Callable[[str], str]
PrintFn = Callable[[str], None]


def template(kalshi_id: str, pm_id: str) -> str:
    """Returns the series template of a pair, e.g. KXBTCY <-> cpc-btc-pricerange-yr.

    The Kalshi part is the series prefix; the PM part is the slug up to its first all-digit
    segment.
    """
    pm_parts = []
    for part in pm_id.split("-"):
        if part.isdigit():
            break
        pm_parts.append(part)
    return f"{kalshi_id.split('-', 1)[0]} <-> {'-'.join(pm_parts) or pm_id}"


def sample_rows(rows: Sequence[sqlite3.Row], per_template: int) -> list[sqlite3.Row]:
    """Keeps the first per_template rows of each template, preserving the queue order."""
    seen: dict[str, int] = {}
    out = []
    for row in rows:
        key = template(row["kalshi_id"], row["pm_id"])
        if seen.get(key, 0) < per_template:
            seen[key] = seen.get(key, 0) + 1
            out.append(row)
    return out


def _fmt_time(m: Market) -> str:
    """Returns a market's close and expiration times as short UTC strings."""
    close = m.close_time.strftime("%Y-%m-%d %H:%M UTC") if m.close_time else "?"
    exp = m.expiration_time.strftime("%Y-%m-%d %H:%M UTC") if m.expiration_time else "?"
    return f"closes {close} · expires {exp}"


def _market_block(label: str, m: Market | None, full: bool) -> list[str]:
    """Returns the display lines for one side of a pair."""
    if m is None:
        return [f"{label}: market snapshot missing"]
    rules = m.rules
    if not full and len(rules) > RULES_PREVIEW:
        rules = rules[:RULES_PREVIEW] + "…"
    lines = [
        f"{label}  {m.venue_market_id}",
        f"  title: {m.title}",
        f"  YES:   {m.yes_label or '-'}",
        f"  {_fmt_time(m)}",
        "  rules:",
    ]
    wrapped = textwrap.wrap(rules, WIDTH, initial_indent="    ", subsequent_indent="    ")
    lines += wrapped or ["    -"]
    return lines


def render(
    row: sqlite3.Row, kalshi: Market | None, pm: Market | None, *, full: bool = False
) -> str:
    """Returns the review screen text for one match row."""
    scores = json.loads(row["scores"] or "{}")
    flags = json.loads(row["flags"] or "[]")
    reasons = json.loads(row["reasons"] or "[]")
    head = (
        f"template {template(row['kalshi_id'], row['pm_id'])}\n"
        f"status {row['status']} · relation {row['relation'] or '-'} · "
        f"polarity {row['polarity'] or '?'} · similarity {row['similarity'] or 0:.2f}"
    )
    if row["obs_label"] is not None:
        ann = f" · {row['obs_ann'] * 100:.1f}%/yr" if row["obs_ann"] is not None else ""
        top = f" · top-of-book {row['obs_top'] * 100:+.2f}¢" if row["obs_top"] is not None else ""
        edge = (
            f"edge {row['obs_label']}: {row['obs_npc'] * 100:+.2f}¢ x {row['obs_contracts']:.0f}"
            f" = ${row['obs_net']:.2f}{ann}{top}"
        )
    else:
        edge = "edge: not priced"
    lines = [head, edge, ""]
    lines += [*_market_block("KALSHI", kalshi, full), "", *_market_block("PM US ", pm, full)]
    if scores:
        lines += [
            "",
            "Jev   same_event {:.2f} (swapped {:.2f}) · polarity {} {:.2f} (swapped {}) · "
            "same_source {:.2f} · deadline {} {:.2f} · divergence {:.2f} · combined {:.2f}".format(
                scores["same_event"], scores["same_event_swapped"], scores["polarity"],
                scores["polarity_p"], scores["polarity_swapped"], scores["same_source"],
                scores["deadline"], scores["deadline_p"], scores["divergence"], scores["combined"],
            ),
        ]
        kinds = scores.get("divergence_kinds") or {}
        if flags:
            flagged = ", ".join(f"{k} ({kinds.get(k, 0):.2f})" for k in flags)
            lines.append(f"      flagged: {flagged}")
    if reasons:
        lines.append("why:  " + "; ".join(reasons))
    if row["note"]:
        lines.append(f"note: {row['note']}")
    return "\n".join(lines)


def _ask(input_fn: InputFn, prompt: str, allowed: str, default: str | None = None) -> str:
    """Prompts until the answer is one of the allowed single characters."""
    while True:
        ans = input_fn(prompt).strip().lower()
        if not ans and default:
            return default
        if ans and ans[0] in allowed:
            return ans[0]


def review(
    store: MatchStore,
    statuses: Sequence[str] = DEFAULT_STATUSES,
    *,
    limit: int | None = None,
    sample: int | None = None,
    match: str | None = None,
    input_fn: InputFn = input,
    print_fn: PrintFn = print,
) -> dict[str, int]:
    """Walks the review queue interactively and records decisions; returns counts by action.

    With sample, only the top `sample` pairs of each series template are shown, best edge first.
    """
    rows = store.review_queue(statuses, None if sample or match else limit)
    if match:
        pattern = re.compile(match, re.IGNORECASE)
        rows = [r for r in rows if pattern.search(f"{r['kalshi_id']} {r['pm_id']}")]
        if not sample and limit:
            rows = rows[:limit]
    if sample:
        rows = sample_rows(rows, sample)[:limit] if limit else sample_rows(rows, sample)
    counts = {"equivalent": 0, "implication": 0, "rejected": 0, "uncertain": 0, "skipped": 0}
    if not rows:
        print_fn(f"Nothing to review in: {', '.join(statuses)}")
        return counts
    for i, row in enumerate(rows, 1):
        kalshi = store.get_market(str(Venue.KALSHI), row["kalshi_id"])
        pm = store.get_market(str(Venue.POLYMARKET_US), row["pm_id"])
        full = False
        while True:
            screen = render(row, kalshi, pm, full=full)
            print_fn("\n" + "─" * WIDTH + f"\n[{i}/{len(rows)}] {screen}")
            action = _ask(
                input_fn,
                "\n[e] equivalent  [i] implication  [r] reject  [u] uncertain  [s] skip  "
                "[v] full rules  [q] quit > ",
                "eirusvq",
            )
            if action != "v":
                break
            full = True
        if action == "q":
            break
        if action == "s":
            counts["skipped"] += 1
            continue
        note = input_fn("note (enter to skip) > ").strip() or None
        kid, pid = row["kalshi_id"], row["pm_id"]
        if action == "e":
            default = "s" if row["polarity"] != "inverted" else "i"
            pol = _ask(
                input_fn,
                f"Kalshi YES pays when PM YES pays? [s]ame / [i]nverted (default {default}) > ",
                "si",
                default,
            )
            store.record_decision(
                kid, pid, "approved_equivalent", relation="equivalent",
                polarity="same" if pol == "s" else "inverted", note=note,
            )
            counts["equivalent"] += 1
        elif action == "i":
            default = "k" if row["direction"] == "kalshi_broader" else "p"
            side = _ask(
                input_fn,
                f"Which market is broader (pays YES in more cases)? [k]alshi / [p]m "
                f"(default {default}) > ",
                "kp",
                default,
            )
            store.record_decision(
                kid, pid, "approved_implication", relation="implication", polarity="same",
                direction="kalshi_broader" if side == "k" else "pm_broader", note=note,
            )
            counts["implication"] += 1
        elif action == "r":
            store.record_decision(kid, pid, "rejected", note=note)
            counts["rejected"] += 1
        elif action == "u":
            store.record_decision(kid, pid, "uncertain", relation=row["relation"],
                                  polarity=row["polarity"], direction=row["direction"], note=note)
            counts["uncertain"] += 1
    return counts
