from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from moneymaker.matching.embed import Embedder, market_text
from moneymaker.matching.extract import Check, compare, compare_methods, describe
from moneymaker.models import Market


@dataclass(frozen=True)
class Candidate:
    """A Kalshi/PM pair proposed for classification, with its similarity and shape check."""

    kalshi: Market
    pm: Market
    similarity: float
    check: Check


@dataclass
class CandidateStats:
    """Counts of how candidate pairs were produced and filtered."""

    kalshi_markets: int = 0
    pm_markets: int = 0
    proposed: int = 0
    kept: int = 0
    rejected: Counter = field(default_factory=Counter)


def settle_time(market: Market) -> datetime | None:
    """Returns the market's expiration time, falling back to its close time."""
    return market.expiration_time or market.close_time


def generate(
    kalshi: list[Market],
    pm: list[Market],
    embedder: Embedder,
    *,
    window_days: float = 30.0,
    top_k: int = 5,
    max_scan: int = 300,
) -> tuple[list[Candidate], CandidateStats]:
    """Proposes, for each PM market, the top_k most similar Kalshi markets that pass shape checks.

    Kalshi markets settling within window_days are scanned in similarity order, up to max_scan
    of them. Pairs whose strike structure or participants conflict are skipped and counted.
    """
    stats = CandidateStats(kalshi_markets=len(kalshi), pm_markets=len(pm))
    if not kalshi or not pm:
        return [], stats

    kv = embedder.embed([market_text(m) for m in kalshi])
    pv = embedder.embed([market_text(m) for m in pm])
    sims = pv @ kv.T

    k_times = np.array(
        [t.timestamp() if (t := settle_time(m)) else np.nan for m in kalshi], dtype=np.float64
    )
    k_specs = [describe(m) for m in kalshi]
    window_s = window_days * 86400

    out: list[Candidate] = []
    for i, pm_market in enumerate(pm):
        row = sims[i].copy()
        t = settle_time(pm_market)
        if t is not None:
            far = np.abs(k_times - t.timestamp()) > window_s
            row[far & ~np.isnan(k_times)] = -np.inf
        n = min(max_scan, int(np.isfinite(row).sum()))
        if n == 0:
            continue
        scan = np.argpartition(-row, n - 1)[:n]
        pm_spec = describe(pm_market)
        kept = 0
        for j in scan[np.argsort(-row[scan])]:
            stats.proposed += 1
            check = compare(k_specs[j], pm_spec)
            if check.result != "reject":
                method_check = compare_methods(kalshi[j], pm_market)
                if method_check.result == "reject":
                    check = method_check
            if check.result == "reject":
                stats.rejected[check.reason] += 1
                continue
            stats.kept += 1
            kept += 1
            out.append(Candidate(kalshi[j], pm_market, float(row[j]), check))
            if kept >= top_k:
                break
    return out, stats
