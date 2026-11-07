from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import structlog

from moneymaker.config import Settings, get_settings
from moneymaker.edge import Edge, Leg, best_edge, price_pair
from moneymaker.fees import FeeModel
from moneymaker.matching.autoapprove import AUTO_STATUSES, AutoApprover
from moneymaker.matching.candidates import Candidate, CandidateStats, generate
from moneymaker.matching.embed import MODEL_NAME as LOCAL_EMBED_MODEL
from moneymaker.matching.embed import Embedder
from moneymaker.matching.gemini import GeminiEncoder
from moneymaker.matching.jev import PROMPT_VERSION, Answers, JevClient, inputs_hash
from moneymaker.matching.openrouter import OpenRouterEncoder
from moneymaker.matching.routing import Thresholds, route
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
IMPLICATION_RELATIONS = ("implication_candidate", "implication")


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
    jev_calls: int = 0
    jev_cached: int = 0
    embedded: int = 0
    embedding_model: str = ""
    report_path: Path | None = None

    def status_counts(self) -> Counter:
        """Returns how many pairs ended in each status."""
        return Counter(r.status for r in self.results)


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
    scope: str,
    kalshi: KalshiClient,
    pm: PolymarketUSClient,
    *,
    only_series: Collection[str] | None = None,
) -> tuple[list[Market], list[Market], FeeModel]:
    """Fetches open markets in scope from both venues and builds the Kalshi fee model.

    With only_series, only those in-scope Kalshi series are fetched.
    """
    pm_categories, kalshi_categories = SCOPES[scope]
    series = await kalshi.list_series()
    multipliers = {
        s["ticker"]: Decimal(str(s.get("fee_multiplier", 1)))
        for s in series
        if s.get("fee_multiplier") is not None
    }
    tickers = [
        s["ticker"] for s in series
        if s.get("category") in kalshi_categories
        and (only_series is None or s["ticker"] in only_series)
    ]
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


def filter_markets(markets: list[Market], include: str | None) -> list[Market]:
    """Returns the markets whose id or title matches the include regex, or all without one."""
    if not include:
        return markets
    pattern = re.compile(include, re.IGNORECASE)
    return [m for m in markets if pattern.search(f"{m.venue_market_id} {m.title}")]


def remote_encoder(settings: Settings) -> GeminiEncoder | OpenRouterEncoder | None:
    """Returns the configured remote embedding encoder, or None when its API key is missing.

    Models named gemini-* use the Gemini API; models with a provider prefix use OpenRouter.
    """
    model = settings.embedding_model
    if model.startswith("gemini-") and settings.gemini_api_key:
        return GeminiEncoder(settings.gemini_api_key.get_secret_value(), model)
    if "/" in model and settings.openrouter_api_key:
        return OpenRouterEncoder(settings.openrouter_api_key.get_secret_value(), model)
    return None


def legs_for(
    status: str, routed: Routed, c: Candidate, kb: OrderBook, pb: OrderBook
) -> tuple[str, Leg, Leg] | str | None:
    """Returns one fixed (label, leg, leg) for implications, a polarity for equivalents, or None.

    Both routed implication candidates and user-approved implications use the safe direction.
    """
    if routed.relation in IMPLICATION_RELATIONS:
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


def auto_approve(
    approver: AutoApprover, store: MatchStore, c: Candidate, routed: Routed
) -> Routed:
    """Returns an auto_approved equivalent when the approver accepts an undecided pair.

    Pairs outside uncertain/pending_review, or with a stored user decision, are returned as is.
    """
    if not approver.enabled or routed.status not in AUTO_STATUSES:
        return routed
    row = store.get_match(c.kalshi.venue_market_id, c.pm.venue_market_id)
    if row is not None and row["decided_by"] == "user":
        return routed
    decision = approver.check(c.kalshi, c.pm, c.check, routed)
    if not decision.approved:
        return routed
    return Routed(
        "auto_approved", relation="equivalent", polarity=decision.polarity,
        scores=routed.scores, flags=routed.flags, reasons=decision.reasons,
    )


async def run_match(
    scope: str,
    *,
    settings: Settings | None = None,
    use_jev: bool = True,
    jev_limit: int | None = None,
    window_days: float = 30.0,
    top_k: int = 5,
    min_edge_per_contract: Decimal = Decimal("0.002"),
    report_dir: Path = Path("data/match_runs"),
    thresholds: Thresholds | None = None,
    include: str | None = None,
) -> MatchRun:
    """Runs fetch, candidate search, Jev classification, routing, pricing and reporting."""
    settings = settings or get_settings()
    thresholds = thresholds or Thresholds.load()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    store = MatchStore(settings.match_db_path)
    kalshi, pm = KalshiClient(settings), PolymarketUSClient(settings)
    jev: JevClient | None = None
    if use_jev and settings.typesafe_api_key:
        jev = JevClient(settings.typesafe_api_key.get_secret_value(), settings.typesafe_model)
    try:
        log.info("match.fetching", scope=scope)
        k_markets, p_markets, fee_model = await fetch_markets(scope, kalshi, pm)
        k_markets = filter_markets(k_markets, include)
        p_markets = filter_markets(p_markets, include)
        store.put_markets(k_markets + p_markets)
        log.info("match.fetched", kalshi=len(k_markets), pm=len(p_markets))

        encoder = remote_encoder(settings)
        embedder = None
        if encoder is not None:
            embedder = Embedder(store, settings.embedding_model, encode=encoder)
            try:
                candidates, stats = generate(
                    k_markets, p_markets, embedder, window_days=window_days, top_k=top_k
                )
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                log.warning("match.embedding_failed_using_local_model",
                            model=settings.embedding_model, error=repr(exc)[:200])
                embedder = None
            finally:
                encoder.close()
        else:
            log.warning("match.no_embedding_key_using_local_model", model=LOCAL_EMBED_MODEL)
        if embedder is None:
            embedder = Embedder(store, LOCAL_EMBED_MODEL)
            candidates, stats = generate(
                k_markets, p_markets, embedder, window_days=window_days, top_k=top_k
            )
        run = MatchRun(
            run_id, scope, stats, embedded=embedder.computed, embedding_model=embedder.model_name
        )
        log.info("match.candidates", kept=stats.kept, proposed=stats.proposed)
        approver = AutoApprover.from_store(store, thresholds)
        log.info("match.auto_templates", templates=len(approver.templates),
                 enabled=approver.enabled)

        async def classify(c: Candidate) -> PairResult:
            """Classifies and routes one candidate, using cached answers when available."""
            h = inputs_hash(c.kalshi, c.pm)
            kid, pid = c.kalshi.venue_market_id, c.pm.venue_market_id
            if jev is None:
                routed = Routed("unclassified", reasons=["not classified by Jev"])
            else:
                cached = store.get_classification(kid, pid, h, jev.model, PROMPT_VERSION)
                if cached is not None:
                    answers = Answers(model=cached["model"], raw=cached["answers"])
                    run.jev_cached += 1
                elif jev_limit is not None and run.jev_calls >= jev_limit:
                    routed = Routed("unclassified", reasons=["Jev call limit reached"])
                    status = store.upsert_routed(kid, pid, h, c.similarity, routed)
                    return PairResult(c, status, routed)
                else:
                    run.jev_calls += 1
                    try:
                        answers = await jev.classify(c.kalshi, c.pm)
                    except httpx.HTTPError as exc:
                        log.warning("match.jev_error", kalshi=kid, pm=pid, error=repr(exc))
                        routed = Routed("unclassified", reasons=[f"Jev error: {exc!r}"[:200]])
                        status = store.upsert_routed(kid, pid, h, c.similarity, routed)
                        return PairResult(c, status, routed)
                    store.put_classification(
                        kid, pid, h, jev.model, PROMPT_VERSION,
                        {"model": answers.model, "answers": answers.raw},
                    )
                routed = route(answers, c.check, thresholds)
                routed = auto_approve(approver, store, c, routed)
            status = store.upsert_routed(kid, pid, h, c.similarity, routed)
            row = store.get_match(kid, pid)
            if row is not None and row["decided_by"] == "user":
                routed = Routed(
                    status, row["relation"], row["polarity"], row["direction"],
                    routed.scores, routed.flags, routed.reasons,
                )
            return PairResult(c, status, routed)

        run.results = list(await asyncio.gather(*(classify(c) for c in candidates)))

        books: dict[tuple[str, str], asyncio.Future[OrderBook]] = {}
        sem = asyncio.Semaphore(4)

        async def fetch_book(venue: str, market_id: str) -> OrderBook:
            """Fetches one REST order book."""
            async with sem:
                client = kalshi if venue == "kalshi" else pm
                return await client.get_orderbook(market_id)

        async def book(venue: str, market_id: str) -> OrderBook:
            """Returns one REST order book, sharing a single fetch among concurrent callers."""
            key = (venue, market_id)
            if key not in books:
                books[key] = asyncio.create_task(fetch_book(venue, market_id))
            return await books[key]

        pm_needed = sorted({
            r.candidate.pm.venue_market_id for r in run.results if r.status not in UNPRICED
        })
        if pm.signer is not None and pm_needed:
            ws_books = await pm.snapshot_books(pm_needed)
            log.info("match.pm_ws_books", got=len(ws_books), wanted=len(pm_needed))
            for slug, b in ws_books.items():
                done: asyncio.Future[OrderBook] = asyncio.get_running_loop().create_future()
                done.set_result(b)
                books[("pm", slug)] = done

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
        if jev is not None:
            await jev.aclose()
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
