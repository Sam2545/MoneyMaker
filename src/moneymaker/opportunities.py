from __future__ import annotations

import json
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from moneymaker.edge import Edge

ONE = Decimal(1)
BUCKETS = (("< 1 s", 1.0), ("1-5 s", 5.0), ("5-60 s", 60.0), ("> 60 s", float("inf")))
TOP_N = 10
LONGEST_N = 5
MERGE_GAP_S = 120.0
MIN_SECONDS = 2.0
PAIR_WIDTH = 28
GAP = "  "
LEGEND = (
    "¢/ct: net cents per contract · n: raw episodes merged · capital: dollars locked at max $",
    "max ct: contracts the books could fill at the best moment, using half the visible depth",
    "max $: theoretical profit at that moment after fees, assuming instant fills",
)


def iso(t: datetime) -> str:
    """Returns a UTC datetime as an ISO-8601 string with millisecond precision."""
    return t.isoformat(timespec="milliseconds")


def is_live(edge: Edge | None) -> bool:
    """Returns True when an edge sizes at least one contract with positive net dollars."""
    return edge is not None and edge.contracts >= ONE and edge.net > 0


@dataclass
class Episode:
    """One continuous stretch during which a pair had a sized, profitable edge."""

    kalshi_id: str
    pm_id: str
    asset: str
    template: str
    started_at: datetime
    label: str = ""
    ended_at: datetime | None = None
    max_cents_per_contract: float = 0.0
    max_contracts: float = 0.0
    max_net: float = 0.0
    capital_at_max: float = 0.0
    annualized_at_max: float | None = None
    updates: int = 0

    def observe(self, edge: Edge) -> None:
        """Folds one live edge into the episode's maxima."""
        self.updates += 1
        self.max_cents_per_contract = max(
            self.max_cents_per_contract, float(edge.net_per_contract) * 100
        )
        self.max_contracts = max(self.max_contracts, float(edge.contracts))
        if self.updates == 1 or float(edge.net) > self.max_net:
            self.max_net = float(edge.net)
            self.label = edge.label
            self.capital_at_max = float(edge.capital)
            self.annualized_at_max = None if edge.annualized is None else float(edge.annualized)

    def row(self) -> dict[str, Any]:
        """Returns the episode as a dict keyed by the opportunities table columns."""
        end = self.ended_at or self.started_at
        return {
            "kalshi_id": self.kalshi_id, "pm_id": self.pm_id, "label": self.label,
            "asset": self.asset, "template": self.template,
            "started_at": iso(self.started_at), "ended_at": iso(end),
            "duration_s": (end - self.started_at).total_seconds(),
            "max_cents_per_contract": self.max_cents_per_contract,
            "max_contracts": self.max_contracts, "max_net": self.max_net,
            "capital_at_max": self.capital_at_max, "annualized_at_max": self.annualized_at_max,
            "updates": self.updates,
        }


PairKey = tuple[str, str]


class EpisodeTracker:
    """Opens an episode when a pair's edge turns live and closes it when the edge stops."""

    def __init__(self, on_close: Callable[[Episode], None]) -> None:
        """Stores the callback that receives each closed episode."""
        self.on_close = on_close
        self.open: dict[PairKey, Episode] = {}
        self.closed = 0

    def update(
        self, key: PairKey, edge: Edge | None, now: datetime, *, asset: str = "",
        template: str = "",
    ) -> None:
        """Feeds one recomputed edge for a pair, opening, extending or closing its episode."""
        episode = self.open.get(key)
        if is_live(edge):
            assert edge is not None
            if episode is None:
                episode = self.open[key] = Episode(key[0], key[1], asset, template, now)
            episode.observe(edge)
        elif episode is not None:
            self.close(key, now)

    def close(self, key: PairKey, now: datetime) -> None:
        """Closes a pair's open episode at now, if any, and hands it to the callback."""
        episode = self.open.pop(key, None)
        if episode is None:
            return
        episode.ended_at = now
        self.closed += 1
        self.on_close(episode)

    def close_all(self, now: datetime) -> None:
        """Closes every open episode at now."""
        for key in list(self.open):
            self.close(key, now)


def bucket(duration_s: float) -> str:
    """Returns the duration bucket label of an episode length in seconds."""
    return next(name for name, upper in BUCKETS if duration_s < upper)


def parse_iso(s: str) -> datetime:
    """Parses an ISO-8601 timestamp into an aware UTC datetime."""
    t = datetime.fromisoformat(s)
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(UTC)


def merge_episodes(rows: Iterable[dict[str, Any]], gap_s: float) -> list[dict[str, Any]]:
    """Merges consecutive episodes of the same pair and direction that start within gap_s of
    the previous end, returning merged rows sorted by start with a merged count per row."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[(r["kalshi_id"], r["pm_id"], r["label"])].append(r)
    merged: list[dict[str, Any]] = []
    for group in groups.values():
        group.sort(key=lambda r: r["started_at"])
        current: dict[str, Any] | None = None
        for r in group:
            start, end = parse_iso(r["started_at"]), parse_iso(r["ended_at"])
            if current is not None and (start - current["_end"]).total_seconds() <= gap_s:
                absorb(current, r, end)
            else:
                if current is not None:
                    merged.append(finish(current))
                current = {**r, "merged": 1, "_start": start, "_end": end}
        if current is not None:
            merged.append(finish(current))
    return sorted(merged, key=lambda r: r["started_at"])


def absorb(current: dict[str, Any], r: dict[str, Any], end: datetime) -> None:
    """Folds episode row r, which ends at end, into the merged row current."""
    current["merged"] += 1
    current["updates"] += r["updates"]
    if end > current["_end"]:
        current["_end"], current["ended_at"] = end, r["ended_at"]
    for key in ("max_cents_per_contract", "max_contracts"):
        current[key] = max(current[key], r[key])
    if r["max_net"] > current["max_net"]:
        for key in ("max_net", "capital_at_max", "annualized_at_max"):
            current[key] = r[key]


def finish(current: dict[str, Any]) -> dict[str, Any]:
    """Drops the working fields of a merged row and sets its start-to-end duration."""
    start, end = current.pop("_start"), current.pop("_end")
    current["duration_s"] = (end - start).total_seconds()
    return current


def group_counts(
    merged: list[dict[str, Any]], kept: list[dict[str, Any]], field_name: str
) -> dict[str, dict[str, float]]:
    """Returns, per value of field_name, the merged count, kept count and kept max-net dollars."""
    agg: dict[str, dict[str, float]] = defaultdict(
        lambda: {"episodes": 0, "filtered": 0, "filtered_net": 0.0}
    )
    for r in merged:
        agg[r[field_name] or "-"]["episodes"] += 1
    for r in kept:
        g = agg[r[field_name] or "-"]
        g["filtered"] += 1
        g["filtered_net"] = round(g["filtered_net"] + r["max_net"], 4)
    return dict(sorted(agg.items(), key=lambda kv: (-kv[1]["filtered_net"], -kv[1]["episodes"])))


def summarize(
    rows: Iterable[dict[str, Any]], *, start: str, end: str, merge_gap_s: float = MERGE_GAP_S,
    min_seconds: float = MIN_SECONDS,
) -> dict[str, Any]:
    """Aggregates raw episode rows by max-net dollars, plus merged_* and filtered_* views of
    episodes merged across gaps up to merge_gap_s and kept when lasting at least min_seconds."""
    rows = list(rows)
    nets = [r["max_net"] for r in rows]
    groups: dict[str, dict[str, dict[str, float]]] = {"asset": {}, "template": {}}
    for field_name, table in groups.items():
        agg: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "total_net": 0.0})
        for r in rows:
            g = agg[r[field_name] or "-"]
            g["count"] += 1
            g["total_net"] += r["max_net"]
        table.update(sorted(agg.items(), key=lambda kv: -kv[1]["count"]))
    top = sorted(rows, key=lambda r: r["max_net"], reverse=True)[:TOP_N]
    merged = merge_episodes(rows, merge_gap_s)
    kept = [r for r in merged if r["duration_s"] >= min_seconds]
    kept_nets = [r["max_net"] for r in kept]
    return {
        "start": start,
        "end": end,
        "episodes": len(rows),
        "total_net": round(sum(nets), 4),
        "median_net": round(statistics.median(nets), 4) if nets else 0.0,
        "by_asset": groups["asset"],
        "by_template": groups["template"],
        "durations": durations(rows),
        "top": top,
        "merge_gap_s": merge_gap_s,
        "min_seconds": min_seconds,
        "merged_episodes": len(merged),
        "merged_durations": durations(merged),
        "filtered_episodes": len(kept),
        "filtered_total_net": round(sum(kept_nets), 4),
        "filtered_median_net": round(statistics.median(kept_nets), 4) if kept_nets else 0.0,
        "filtered_by_asset": group_counts(merged, kept, "asset"),
        "filtered_by_template": group_counts(merged, kept, "template"),
        "filtered_top": sorted(kept, key=lambda r: r["max_net"], reverse=True)[:TOP_N],
        "filtered_longest": sorted(kept, key=lambda r: r["duration_s"], reverse=True)[:LONGEST_N],
    }


def durations(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Counts rows per duration bucket."""
    counts = {name: 0 for name, _ in BUCKETS}
    for r in rows:
        counts[bucket(r["duration_s"])] += 1
    return counts


def short_time(s: str) -> str:
    """Formats an ISO-8601 timestamp as UTC MM-DD HH:MM:SS."""
    return parse_iso(s).strftime("%m-%d %H:%M:%S")


def fit(text: str, width: int) -> str:
    """Shortens text to width characters by replacing its middle with an ellipsis."""
    if len(text) <= width:
        return text
    head = (width - 1) // 2
    return text[:head] + "…" + text[len(text) - (width - 1 - head):]


def render_table(
    headers: list[str], rows: list[list[str]], *, right: set[int] | None = None,
    sublines: list[str] | None = None, sub_col: int = 0,
) -> list[str]:
    """Renders rows as space-padded fixed-width lines under an underlined header.

    Columns in right are right-aligned. Each sublines entry is printed under its row starting
    at column sub_col and is cut to the table width.
    """
    right = right or set()
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]

    def line(cells: list[str]) -> str:
        """Joins cells padded to their column widths."""
        return GAP.join(
            c.rjust(w) if i in right else c.ljust(w)
            for i, (c, w) in enumerate(zip(cells, widths, strict=True))
        )

    total = sum(widths) + len(GAP) * (len(widths) - 1)
    offset = sum(widths[:sub_col]) + len(GAP) * sub_col
    out = [line(headers), GAP.join("─" * w for w in widths)]
    for i, r in enumerate(rows):
        out.append(line(r))
        if sublines is not None:
            out.append((" " * offset + fit(sublines[i], total - offset)).ljust(total))
    return out


def episode_table(rows: list[dict[str, Any]]) -> list[str]:
    """Renders episodes with the Kalshi id on the first line and the PM id on the second."""
    if not rows:
        return ["(none)"]
    cells = []
    for r in rows:
        ann = "-" if r["annualized_at_max"] is None else f"{r['annualized_at_max'] * 100:,.1f}"
        cells.append([
            short_time(r["started_at"]), fit(r["kalshi_id"], PAIR_WIDTH), r["label"],
            f"{r['duration_s']:,.1f}", f"{r.get('merged', 1):,}",
            f"{r['max_cents_per_contract']:.2f}", f"{r['max_contracts']:,.0f}",
            f"{r['max_net']:,.2f}", f"{r['capital_at_max']:,.2f}", ann,
        ])
    return render_table(
        ["Start (UTC)", "Kalshi / ↳ PM", "Direction", "Dur s", "n", "¢/ct", "Max ct", "Max $",
         "Capital", "%/yr"],
        cells, right=set(range(3, 10)), sublines=["↳ " + r["pm_id"] for r in rows], sub_col=1,
    )


def group_table(title: str, groups: dict[str, dict[str, float]], min_label: str) -> list[str]:
    """Renders per-group filtered/total counts and filtered max-net dollars."""
    cells = [[name, f"{g['filtered']:,.0f} / {g['episodes']:,.0f}", f"{g['filtered_net']:,.2f}"]
             for name, g in groups.items()]
    return render_table([title, f"{min_label} / all", "Max $"], cells, right={1, 2})


def render_text(summary: dict[str, Any], *, show_all: bool = False) -> str:
    """Renders a summary from summarize as an aligned plain-text report for a terminal."""
    s = summary
    min_label = "shown" if show_all else f"≥ {s['min_seconds']:g} s"
    kept = "all shown" if show_all else f"{s['filtered_episodes']:,} lasting {min_label}"
    lines = [
        f"Opportunities {short_time(s['start'])} → {short_time(s['end'])} UTC",
        "",
        f"{s['episodes']:,} raw episodes → {s['merged_episodes']:,} merged "
        f"(gaps ≤ {s['merge_gap_s']:g} s) → {kept}",
        f"Max $ of {'all merged' if show_all else 'kept'} episodes: "
        f"${s['filtered_total_net']:,.2f} total · ${s['filtered_median_net']:,.2f} median",
        "",
        "Durations (all merged episodes)",
        *render_table(["Duration", "Episodes"],
                      [[k, f"{v:,}"] for k, v in s["merged_durations"].items()], right={1}),
        "",
        "By asset",
        *group_table("Asset", s["filtered_by_asset"], min_label),
        "",
        "By template",
        *group_table("Template", s["filtered_by_template"], min_label),
        "",
        f"Top {TOP_N} by max $",
        *episode_table(s["filtered_top"]),
        *LEGEND,
        "",
        f"Longest {LONGEST_N} by duration",
        *episode_table(s["filtered_longest"]),
    ]
    return "\n".join(lines) + "\n"


def render_markdown(summary: dict[str, Any]) -> str:
    """Renders a summary as Markdown holding the plain-text report in a code block."""
    return f"# Opportunities {summary['start']} to {summary['end']}\n\n```\n" \
        f"{render_text(summary)}```\n"


def write_daily(summary: dict[str, Any], log_dir: Path, day: str) -> tuple[Path, Path]:
    """Writes opportunities-<day>.json and .md into log_dir and returns both paths."""
    log_dir.mkdir(parents=True, exist_ok=True)
    json_path = log_dir / f"opportunities-{day}.json"
    md_path = log_dir / f"opportunities-{day}.md"
    json_path.write_text(json.dumps(summary, indent=2) + "\n")
    md_path.write_text(render_markdown(summary))
    return json_path, md_path


def local_day_bounds(t: datetime) -> tuple[datetime, datetime]:
    """Returns the UTC instants of local midnight on t's local date and of the next midnight."""
    d = t.astimezone().date()
    n = d + timedelta(days=1)
    start = datetime(d.year, d.month, d.day).astimezone()
    end = datetime(n.year, n.month, n.day).astimezone()
    return start.astimezone(UTC), end.astimezone(UTC)
