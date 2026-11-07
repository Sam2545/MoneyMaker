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


def summarize(rows: Iterable[dict[str, Any]], *, start: str, end: str) -> dict[str, Any]:
    """Aggregates episode rows into counts per asset and template, durations, dollars and top 10.

    Dollar figures use each episode's maximum net.
    """
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
    durations = {name: 0 for name, _ in BUCKETS}
    for r in rows:
        durations[bucket(r["duration_s"])] += 1
    top = sorted(rows, key=lambda r: r["max_net"], reverse=True)[:TOP_N]
    return {
        "start": start,
        "end": end,
        "episodes": len(rows),
        "total_net": round(sum(nets), 4),
        "median_net": round(statistics.median(nets), 4) if nets else 0.0,
        "by_asset": groups["asset"],
        "by_template": groups["template"],
        "durations": durations,
        "top": top,
    }


def render_markdown(summary: dict[str, Any]) -> str:
    """Renders a summary from summarize as a human-readable Markdown report."""
    s = summary
    lines = [
        f"# Opportunities {s['start']} to {s['end']}",
        "",
        f"- Episodes: {s['episodes']}",
        f"- Total max net: ${s['total_net']:.2f} · median: ${s['median_net']:.2f}",
        "- Durations: " + ", ".join(f"{k} {v}" for k, v in s["durations"].items()),
        "",
    ]
    for title, key in (("Asset", "by_asset"), ("Template", "by_template")):
        lines += [f"| {title} | Episodes | Total $ |", "|---|---:|---:|"]
        lines += [f"| {name} | {g['count']:.0f} | {g['total_net']:.2f} |"
                  for name, g in s[key].items()]
        lines.append("")
    lines += [
        "## Top episodes", "",
        "| Start (UTC) | Pair | Direction | Duration s | Max ¢/ct | Max ct | Max $ | Capital $ "
        "| %/yr |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in s["top"]:
        ann = "-" if r["annualized_at_max"] is None else f"{r['annualized_at_max'] * 100:.1f}"
        lines.append(
            f"| {r['started_at'][:19]} | {r['kalshi_id']} / {r['pm_id']} | {r['label']} | "
            f"{r['duration_s']:.1f} | {r['max_cents_per_contract']:.2f} | "
            f"{r['max_contracts']:.0f} | {r['max_net']:.2f} | {r['capital_at_max']:.2f} | {ann} |"
        )
    return "\n".join(lines) + "\n"


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
