from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from moneymaker.config import Settings, get_settings
from moneymaker.edge import Edge, Leg, best_edge, price_pair
from moneymaker.fees import FeeModel
from moneymaker.matching.candidates import Candidate, CandidateStats, generate
from moneymaker.matching.embed import MODEL_NAME as LOCAL_EMBED_MODEL
from moneymaker.matching.embed import Embedder
from moneymaker.matching.openrouter import OpenRouterEncoder
from moneymaker.matching.store import MatchStore, Routed
from moneymaker.models import Market, MarketStatus, Side
from moneymaker.orderbook import OrderBook
from moneymaker.venues.kalshi.client import KalshiClient
from moneymaker.venues.polymarket_us.client import PolymarketUSClient

log = structlog.get_logger(__name__)

SCOPES: dict[str, tuple[list[str], list[str]]] = {
    "crypto": (["crypto"], ["Crypto"]),
    "economics": (["macro", "finance"], ["Economics", "Financials", "Commodities"]),
    "politics": (["politics"], ["Politics", "Elections", "World"]),
    "sports": (["sports"], ["Sports"]),
    "culture": (["culture"], ["Entertainment", "Culture"]),
    "climate": (["climate"], ["Climate and Weather"]),
    "tech": (["technology"], ["Science and Technology", "Companies"]),
}
UNPRICED = {"rejected"}


@dataclass
class PairResult:
    """One candidate pair after classification, routing and pricing."""

    candidate: Candidate
    status: str
    routed: Routed
    edge: Edge | None = None


@dataclass
class MatchRun:
    """Summary of one match run."""

    run_id: str
    scope: str
    stats: CandidateStats
    results: list[PairResult] = field(default_factory=list)
    embedded: int = 0
    embedding_model: str = ""
    report_path: Path | None = None

    def status_counts(self) -> Counter:
        """Returns how many pairs ended in each status."""
        return Counter(r.status for r in self.results)


def pair_hash(kalshi: Market, pm: Market) -> str:
    """Returns a hash of both markets' normalized fields, so edits are detected."""
    blob = json.dumps(
        [m.model_dump(mode="json", exclude={"raw"}) for m in (kalshi, pm)], sort_keys=True
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def _norm(label: str | None) -> str:
    """Lowercases a label and strips everything except letters and digits."""
    return re.sub(r"[^a-z0-9]", "", (label or "").lower())


def guess_polarity(c: Candidate) -> str | None:
    """Returns 'same' when both YES labels normalize to the same text, else None."""
    a, b = _norm(c.kalshi.yes_label), _norm(c.pm.yes_label)
    if a and a == b:
        return "same"
    pm_yes = _norm(c.pm.yes_label)
    if a and pm_yes and (a in pm_yes or pm_yes in a) and min(len(a), len(pm_yes)) >= 6:
        return "same"
    return None


async def fetch_markets(
    scope: str, kalshi: KalshiClient, pm: PolymarketUSClient
) -> tuple[list[Market], list[Market], FeeModel]:
    """Fetches open markets in scope from both venues and builds the Kalshi fee model."""
    pm_categories, kalshi_categories = SCOPES[scope]
    series = await kalshi.list_series()
    multipliers = {
        s["ticker"]: Decimal(str(s.get("fee_multiplier", 1)))
        for s in series
        if s.get("fee_multiplier") is not None
    }
    tickers = [s["ticker"] for s in series if s.get("category") in kalshi_categories]
    sem = asyncio.Semaphore(8)

    async def one(t: str) -> list[Market]:
        """Fetches the open markets of one Kalshi series."""
        async with sem:
            return await kalshi.list_markets(series_ticker=t)

    kalshi_lists, pm_markets = await asyncio.gather(
        asyncio.gather(*(one(t) for t in tickers)),
        pm.list_markets(categories=pm_categories),
    )
    k = [m for ms in kalshi_lists for m in ms if m.status is MarketStatus.OPEN]
    p = [m for m in pm_markets if m.status is MarketStatus.OPEN]
    return k, p, FeeModel(multipliers)


def legs_for(
    status: str, routed: Routed, c: Candidate, kb: OrderBook, pb: OrderBook
) -> tuple[str, Leg, Leg] | str | None:
    """Returns one fixed (label, leg, leg) for implications, a polarity for equivalents, or None."""
    if routed.relation == "implication_candidate":
        if routed.polarity != "same":
            return None
        if routed.direction == "kalshi_broader":
            k_leg, p_leg = Leg(c.kalshi, kb, Side.YES), Leg(c.pm, pb, Side.NO)
            return ("K-YES + PM-NO (implication)", k_leg, p_leg)
        if routed.direction == "pm_broader":
            k_leg, p_leg = Leg(c.kalshi, kb, Side.NO), Leg(c.pm, pb, Side.YES)
            return ("K-NO + PM-YES (implication)", k_leg, p_leg)
        return None
    if routed.polarity in ("same", "inverted"):
        return routed.polarity
    if status == "unclassified":
        return guess_polarity(c)
    return None


async def run_match(
    scope: str,
    *,
    settings: Settings | None = None,
    window_days: float = 30.0,
    top_k: int = 5,
    min_edge_per_contract: Decimal = Decimal("0.002"),
    report_dir: Path = Path("data/match_runs"),
) -> MatchRun:
    """Runs fetch, candidate search, pricing and reporting; every pair is unclassified."""
    settings = settings or get_settings()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    store = MatchStore(settings.match_db_path)
    kalshi, pm = KalshiClient(settings), PolymarketUSClient(settings)
    try:
        log.info("match.fetching", scope=scope)
        k_markets, p_markets, fee_model = await fetch_markets(scope, kalshi, pm)
        store.put_markets(k_markets + p_markets)
        log.info("match.fetched", kalshi=len(k_markets), pm=len(p_markets))

        encoder: OpenRouterEncoder | None = None
        if settings.openrouter_api_key:
            encoder = OpenRouterEncoder(
                settings.openrouter_api_key.get_secret_value(), settings.embedding_model
            )
            embedder = Embedder(store, settings.embedding_model, encode=encoder)
        else:
            log.warning("match.no_openrouter_key_using_local_model", model=LOCAL_EMBED_MODEL)
            embedder = Embedder(store, LOCAL_EMBED_MODEL)
        try:
            candidates, stats = generate(
                k_markets, p_markets, embedder, window_days=window_days, top_k=top_k
            )
        finally:
            if encoder is not None:
                encoder.close()
        run = MatchRun(
            run_id, scope, stats, embedded=embedder.computed, embedding_model=embedder.model_name
        )
        log.info("match.candidates", kept=stats.kept, proposed=stats.proposed)

        async def classify(c: Candidate) -> PairResult:
            """Records one candidate as unclassified, keeping any stored user decision."""
            kid, pid = c.kalshi.venue_market_id, c.pm.venue_market_id
            routed = Routed("unclassified", reasons=["not classified"])
            status = store.upsert_routed(kid, pid, pair_hash(c.kalshi, c.pm), c.similarity, routed)
            row = store.get_match(kid, pid)
            if row is not None and row["decided_by"] == "user":
                routed = Routed(status, row["relation"], row["polarity"], row["direction"])
            return PairResult(c, status, routed)

        run.results = list(await asyncio.gather(*(classify(c) for c in candidates)))

        books: dict[tuple[str, str], OrderBook] = {}
        sem = asyncio.Semaphore(8)

        async def book(venue: str, market_id: str) -> OrderBook:
            """Fetches and memoizes one REST order book."""
            key = (venue, market_id)
            if key not in books:
                async with sem:
                    client = kalshi if venue == "kalshi" else pm
                    books[key] = await client.get_orderbook(market_id)
            return books[key]

        async def price(r: PairResult) -> None:
            """Prices one pair in its tradable direction(s) and records the observation."""
            if r.status in UNPRICED:
                return
            c = r.candidate
            try:
                kb = await book("kalshi", c.kalshi.venue_market_id)
                pb = await book("pm", c.pm.venue_market_id)
            except Exception as exc:
                log.warning("match.book_error", kalshi=c.kalshi.venue_market_id,
                            pm=c.pm.venue_market_id, error=repr(exc))
                return
            plan = legs_for(r.status, r.routed, c, kb, pb)
            kwargs: dict[str, Any] = {"min_edge_per_contract": min_edge_per_contract}
            if isinstance(plan, tuple):
                label, a, b = plan
                r.edge = price_pair(a, b, fee_model, label=label, **kwargs)
            elif isinstance(plan, str):
                r.edge = best_edge((c.kalshi, kb), (c.pm, pb), plan, fee_model, **kwargs)
            if r.edge is not None:
                kid, pid = c.kalshi.venue_market_id, c.pm.venue_market_id
                store.add_observation(run_id, kid, pid, r.edge)

        await asyncio.gather(*(price(r) for r in run.results))
        run.report_path = write_report(run, report_dir)
        return run
    finally:
        await kalshi.aclose()
        await pm.aclose()
        store.close()


def _f(x: Decimal | None) -> float | None:
    """Converts an optional Decimal to float for JSON."""
    return None if x is None else float(x)


def write_report(run: MatchRun, report_dir: Path) -> Path:
    """Writes one JSON line per pair for a run and returns the file path."""
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{run.run_id}-{run.scope}.jsonl"
    with path.open("w") as fh:
        for r in run.results:
            c, e = r.candidate, r.edge
            fh.write(json.dumps({
                "kalshi_id": c.kalshi.venue_market_id,
                "pm_id": c.pm.venue_market_id,
                "kalshi_title": c.kalshi.title,
                "pm_title": c.pm.title,
                "similarity": round(c.similarity, 4),
                "shape_check": c.check.result,
                "shape_reason": c.check.reason,
                "status": r.status,
                "relation": r.routed.relation,
                "polarity": r.routed.polarity,
                "direction": r.routed.direction,
                "scores": r.routed.scores,
                "flags": r.routed.flags,
                "reasons": r.routed.reasons,
                "edge": None if e is None else {
                    "label": e.label,
                    "contracts": _f(e.contracts),
                    "net": _f(e.net),
                    "net_per_contract": _f(e.net_per_contract),
                    "top_marginal": _f(e.top_marginal),
                    "capital": _f(e.capital),
                    "annualized": _f(e.annualized),
                    "days_to_settle": _f(e.days_to_settle),
                },
            }) + "\n")
    return path
