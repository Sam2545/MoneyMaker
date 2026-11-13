from __future__ import annotations

import asyncio
import json
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from moneymaker.config import Settings, get_settings
from moneymaker.edge import Edge, best_edge, price_pair
from moneymaker.fees import FeeModel, series_ticker
from moneymaker.matching.autoapprove import (
    AutoApprover,
    rules_assets,
    rules_fingerprint,
    settlement_instants,
)
from moneymaker.matching.candidates import Candidate
from moneymaker.matching.extract import Check, Spec, compare, describe
from moneymaker.matching.jev import inputs_hash
from moneymaker.matching.pipeline import fetch_markets, filter_markets, legs_for
from moneymaker.matching.review import template as series_template
from moneymaker.matching.routing import Thresholds
from moneymaker.matching.store import APPROVED, MatchStore, Routed
from moneymaker.models import Market, Venue
from moneymaker.orderbook import OrderBook
from moneymaker.venues.kalshi.client import KalshiClient
from moneymaker.venues.polymarket_us.client import PolymarketUSClient

log = structlog.get_logger(__name__)

NO_AI_REASON = "template match (no AI)"
FULL_REFRESH_S = 6 * 3600.0
LOCKED_STATUSES = ("suspended",)

IndexKey = tuple[frozenset[str], frozenset[datetime]]


@dataclass(frozen=True)
class TemplateMatch:
    """A Kalshi/PM pair whose fingerprints, asset, instants and strike structure line up."""

    kalshi: Market
    pm: Market
    check: Check

    @property
    def template(self) -> str:
        """Returns the series template label of the pair, e.g. KXBTCD <-> cpc-btc-above-hr."""
        return series_template(self.kalshi.venue_market_id, self.pm.venue_market_id)

    @property
    def asset(self) -> str:
        """Returns the comma-joined canonical assets named in the Kalshi rules."""
        return ",".join(sorted(rules_assets(self.kalshi.rules)))


@dataclass
class TemplatePair:
    """One matched pair after the store merge: its status, routing and optional priced edge."""

    match: TemplateMatch
    status: str
    routed: Routed
    edge: Edge | None = None


@dataclass
class TemplateRun:
    """Summary of one template-matching run."""

    run_id: str
    scope: str
    full_fetch: bool
    series: list[str]
    kalshi_markets: int = 0
    pm_markets: int = 0
    pm_in_templates: int = 0
    pairs: list[TemplatePair] = field(default_factory=list)
    refused: Counter = field(default_factory=Counter)
    new_auto: int = 0
    elapsed_s: float = 0.0
    fee_model: FeeModel = field(default_factory=FeeModel)
    open_ids: set[tuple[str, str]] = field(default_factory=set)
    markets: dict[tuple[str, str], Market] = field(default_factory=dict)


def usable_fingerprints(approver: AutoApprover) -> tuple[set[str], set[str]]:
    """Returns the Kalshi and PM fingerprints of templates that can approve pairs."""
    kalshi, pm = set(), set()
    for (kfp, pfp), tpl in approver.templates.items():
        if not tpl.conflicted and tpl.count >= max(1, approver.min_examples):
            kalshi.add(kfp)
            pm.add(pfp)
    return kalshi, pm


def target_series(store: MatchStore, kalshi_fps: set[str]) -> set[str]:
    """Returns the Kalshi series of stored markets that carry a usable fingerprint.

    Series of user-approved pairs are included as well.
    """
    out = set()
    rows = store.db.execute("SELECT market_id, snapshot FROM markets WHERE venue = ?",
                            (str(Venue.KALSHI),))
    for row in rows:
        rules = json.loads(row["snapshot"]).get("rules") or ""
        if rules_fingerprint(rules) in kalshi_fps:
            out.add(series_ticker(row["market_id"]))
    approved = store.db.execute(
        f"SELECT kalshi_id FROM matches WHERE decided_by = 'user' "
        f"AND status IN ({','.join('?' * len(APPROVED))})",
        APPROVED,
    )
    out.update(series_ticker(r["kalshi_id"]) for r in approved)
    return out


def index_kalshi(
    markets: Iterable[Market], kalshi_fps: set[str]
) -> dict[IndexKey, list[tuple[Market, str, Spec]]]:
    """Groups Kalshi markets with a usable fingerprint by (assets, settlement instants).

    Each entry carries the market, its fingerprint and its parsed strike spec.
    """
    index: dict[IndexKey, list[tuple[Market, str, Spec]]] = defaultdict(list)
    for m in markets:
        fp = rules_fingerprint(m.rules)
        if fp not in kalshi_fps:
            continue
        assets, instants = rules_assets(m.rules), settlement_instants(m.rules)
        if assets and instants:
            index[(assets, instants)].append((m, fp, describe(m)))
    return index


def find_matches(
    kalshi: list[Market], pm: list[Market], approver: AutoApprover
) -> tuple[list[TemplateMatch], Counter, int]:
    """Pairs PM markets of approved templates with Kalshi markets, without any AI calls.

    Pairs need equal assets and instants, an ok shape check and a usable fingerprint template;
    returns the matches, refusal counts and the number of PM markets in templates.
    """
    kalshi_fps, pm_fps = usable_fingerprints(approver)
    index = index_kalshi(kalshi, kalshi_fps)
    refused: Counter = Counter()
    out: list[TemplateMatch] = []
    in_templates = 0
    for p in pm:
        pfp = rules_fingerprint(p.rules)
        if pfp not in pm_fps:
            continue
        in_templates += 1
        assets, instants = rules_assets(p.rules), settlement_instants(p.rules)
        group = index.get((assets, instants), []) if assets and instants else []
        if not group:
            refused["no Kalshi market with the same asset and instants"] += 1
            continue
        p_spec = describe(p)
        found = False
        for k, kfp, k_spec in group:
            tpl = approver.templates.get((kfp, pfp))
            if tpl is None or tpl.conflicted or tpl.count < approver.min_examples:
                continue
            check = compare(k_spec, p_spec)
            if check.result != "ok":
                continue
            out.append(TemplateMatch(k, p, check))
            found = True
        if not found:
            refused["no Kalshi market with the same strike in an approved template"] += 1
    return out, refused, in_templates


def merge(
    store: MatchStore, approver: AutoApprover, match: TemplateMatch
) -> tuple[TemplatePair | None, str | None, bool]:
    """Approves one match without Jev and merges it into the store; the caller commits.

    Returns (pair or None, refusal reason, newly auto-approved); user and suspended rows are kept.
    """
    k, p = match.kalshi, match.pm
    kid, pid = k.venue_market_id, p.venue_market_id
    h = inputs_hash(k, p)
    row = store.get_match(kid, pid)
    if row is not None and (row["decided_by"] == "user" or row["status"] in LOCKED_STATUSES):
        if row["decided_by"] == "user" and row["inputs_hash"] != h and row["status"] in APPROVED:
            status = store.upsert_routed(kid, pid, h, 1.0, Routed(
                "uncertain", reasons=[NO_AI_REASON, "rules or dates changed after approval"],
            ), commit=False)
            return TemplatePair(match, status, Routed(status)), None, False
        routed = Routed(row["status"], row["relation"], row["polarity"], row["direction"])
        return TemplatePair(match, row["status"], routed), None, False
    decision = approver.check(k, p, match.check, Routed("uncertain"), require_jev=False)
    if not decision.approved:
        return None, "; ".join(decision.reasons), False
    routed = Routed(
        "auto_approved", relation="equivalent", polarity=decision.polarity,
        scores=json.loads(row["scores"] or "{}") if row is not None else None,
        reasons=[NO_AI_REASON, *decision.reasons],
    )
    unchanged = (
        row is not None and row["status"] == "auto_approved" and row["inputs_hash"] == h
        and row["polarity"] == decision.polarity
    )
    if not unchanged:
        store.upsert_routed(kid, pid, h, 1.0, routed, commit=False)
    return TemplatePair(match, "auto_approved", routed), None, not unchanged


async def fetch_books(
    kalshi: KalshiClient, pm: PolymarketUSClient, kalshi_ids: Iterable[str], pm_ids: Iterable[str]
) -> dict[tuple[str, str], OrderBook]:
    """Fetches both venues' books from WebSocket snapshots, or from REST without keys.

    Markets whose book cannot be fetched, or that send no WebSocket snapshot, are left out.
    """
    books: dict[tuple[str, str], OrderBook] = {}
    kalshi_ids, pm_ids = sorted(set(kalshi_ids)), sorted(set(pm_ids))
    rest: list[tuple[str, str]] = []

    async def ws(venue: str, client: KalshiClient | PolymarketUSClient, ids: list[str]) -> None:
        """Collects one venue's WebSocket snapshots into books."""
        got = await client.snapshot_books(ids)
        books.update({(venue, i): b for i, b in got.items()})
        log.info("templates.ws_books", venue=venue, got=len(got), wanted=len(ids))

    snapshots = []
    for venue, client, ids in (("kalshi", kalshi, kalshi_ids), ("pm", pm, pm_ids)):
        if client.signer is None:
            rest += [(venue, i) for i in ids]
        else:
            snapshots.append(ws(venue, client, ids))
    await asyncio.gather(*snapshots)
    sem = asyncio.Semaphore(4)

    async def one(venue: str, market_id: str) -> None:
        """Fetches one REST book into books, logging failures."""
        async with sem:
            try:
                client = kalshi if venue == "kalshi" else pm
                books[(venue, market_id)] = await client.get_orderbook(market_id)
            except Exception as exc:
                log.warning("templates.book_error", venue=venue, market=market_id,
                            error=repr(exc)[:200])

    await asyncio.gather(*(one(v, i) for v, i in rest))
    return books


def price_edge(
    status: str,
    routed: Routed,
    kalshi: Market,
    pm: Market,
    kb: OrderBook,
    pb: OrderBook,
    fee_model: FeeModel,
    min_edge_per_contract: Decimal,
) -> Edge | None:
    """Prices a pair in its tradable direction(s) the same way the match pipeline does."""
    c = Candidate(kalshi, pm, 1.0, Check("ok"))
    plan = legs_for(status, routed, c, kb, pb)
    kwargs: dict[str, Any] = {"min_edge_per_contract": min_edge_per_contract}
    if isinstance(plan, tuple):
        label, a, b = plan
        return price_pair(a, b, fee_model, label=label, **kwargs)
    if isinstance(plan, str):
        return best_edge((kalshi, kb), (pm, pb), plan, fee_model, **kwargs)
    return None


TRADABLE = ("auto_approved", *APPROVED)


class TemplateMatcher:
    """Runs deterministic template matching for a scope, remembering which series to fetch."""

    def __init__(
        self,
        store: MatchStore,
        scope: str,
        *,
        include: str | None = None,
        thresholds: Thresholds | None = None,
        full_refresh_s: float = FULL_REFRESH_S,
    ) -> None:
        """Stores the scope, include regex, thresholds and the full-refresh interval."""
        self.store = store
        self.scope = scope
        self.include = include
        self.thresholds = thresholds or Thresholds.load()
        self.full_refresh_s = full_refresh_s
        self.series: set[str] | None = None
        self.last_full: float | None = None

    async def run(
        self,
        kalshi: KalshiClient,
        pm: PolymarketUSClient,
        *,
        price: bool = True,
        force_full: bool = False,
        min_edge_per_contract: Decimal = Decimal("0.002"),
    ) -> TemplateRun:
        """Fetches markets, matches and approves template pairs, then optionally prices them.

        Only template series are fetched, except on the first run without known series, when
        forced, or every full_refresh_s, when all scope series are fetched.
        """
        started = time.monotonic()
        run_id = "tpl-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        approver = AutoApprover.from_store(self.store, self.thresholds)
        kalshi_fps, _ = usable_fingerprints(approver)
        if self.series is None:
            self.series = target_series(self.store, kalshi_fps)
        due = self.last_full is not None and started - self.last_full >= self.full_refresh_s
        full = force_full or not self.series or due
        log.info("templates.fetching", scope=self.scope, full=full,
                 series=None if full else len(self.series))
        k_markets, p_markets, fee_model = await fetch_markets(
            self.scope, kalshi, pm, only_series=None if full else self.series
        )
        if full:
            self.last_full = started
            found = {series_ticker(m.venue_market_id) for m in k_markets
                     if rules_fingerprint(m.rules) in kalshi_fps}
            self.series = found | target_series(self.store, set())
        self.last_full = self.last_full or started
        k_markets = filter_markets(k_markets, self.include)
        p_markets = filter_markets(p_markets, self.include)
        self.store.put_markets(k_markets + p_markets)
        run = TemplateRun(
            run_id, self.scope, full, sorted(self.series), len(k_markets), len(p_markets),
            fee_model=fee_model,
        )
        for m in k_markets + p_markets:
            key = ("kalshi" if m.venue is Venue.KALSHI else "pm", m.venue_market_id)
            run.open_ids.add(key)
            run.markets[key] = m
        log.info("templates.fetched", kalshi=len(k_markets), pm=len(p_markets),
                 elapsed_s=round(time.monotonic() - started, 1))
        matches, run.refused, run.pm_in_templates = find_matches(k_markets, p_markets, approver)
        for match in matches:
            pair, reason, new = merge(self.store, approver, match)
            if pair is None:
                run.refused[reason or "refused"] += 1
                continue
            run.pairs.append(pair)
            run.new_auto += new
        self.store.db.commit()
        if price:
            await self._price(run, kalshi, pm, min_edge_per_contract)
        run.elapsed_s = time.monotonic() - started
        log.info("templates.done", pairs=len(run.pairs), new_auto=run.new_auto,
                 elapsed_s=round(run.elapsed_s, 1))
        return run

    async def _price(
        self, run: TemplateRun, kalshi: KalshiClient, pm: PolymarketUSClient,
        min_edge_per_contract: Decimal,
    ) -> None:
        """Prices every tradable pair of a run and records one observation per priced pair."""
        tradable = [p for p in run.pairs if p.status in TRADABLE]
        books = await fetch_books(
            kalshi, pm,
            (p.match.kalshi.venue_market_id for p in tradable),
            (p.match.pm.venue_market_id for p in tradable),
        )
        for pair in tradable:
            kid, pid = pair.match.kalshi.venue_market_id, pair.match.pm.venue_market_id
            kb, pb = books.get(("kalshi", kid)), books.get(("pm", pid))
            if kb is None or pb is None:
                continue
            try:
                pair.edge = price_edge(
                    pair.status, pair.routed, pair.match.kalshi, pair.match.pm, kb, pb,
                    run.fee_model, min_edge_per_contract,
                )
            except Exception as exc:
                log.warning("templates.price_error", kalshi=kid, pm=pid, error=repr(exc)[:200])
                continue
            if pair.edge is not None:
                self.store.add_observation(run.run_id, kid, pid, pair.edge, commit=False)
        self.store.db.commit()


async def run_match_templates(
    scope: str,
    *,
    include: str | None = None,
    settings: Settings | None = None,
    force_full: bool = False,
    min_edge_per_contract: Decimal = Decimal("0.002"),
) -> TemplateRun:
    """Runs one template-matching pass with its own clients and store, pricing every pair."""
    settings = settings or get_settings()
    store = MatchStore(settings.match_db_path)
    kalshi, pm = KalshiClient(settings), PolymarketUSClient(settings)
    try:
        matcher = TemplateMatcher(store, scope, include=include, full_refresh_s=FULL_REFRESH_S)
        return await matcher.run(
            kalshi, pm, price=True, force_full=force_full,
            min_edge_per_contract=min_edge_per_contract,
        )
    finally:
        await kalshi.aclose()
        await pm.aclose()
        store.close()


def summary_lines(run: TemplateRun) -> list[str]:
    """Returns the printable per-asset and per-template summary of a template run."""
    lines = [
        f"scope {run.scope}: {run.kalshi_markets} Kalshi x {run.pm_markets} PM US markets "
        f"({'full fetch' if run.full_fetch else f'{len(run.series)} template series'})",
        f"PM markets in approved templates: {run.pm_in_templates} · pairs: {len(run.pairs)} "
        f"· newly auto-approved: {run.new_auto} · {run.elapsed_s:.1f}s",
    ]
    groups: dict[tuple[str, str], list[TemplatePair]] = defaultdict(list)
    for p in run.pairs:
        groups[(p.match.asset, p.match.template)].append(p)
    if groups:
        lines.append(f"\n{'asset':<6}{'template':<44}{'pairs':>6}{'auto':>6}{'user':>6}"
                     f"{'other':>6}{'edge>0':>7}{'best $':>9}")
        for (asset, tpl), ps in sorted(groups.items()):
            auto = sum(p.status == "auto_approved" for p in ps)
            user = sum(p.status in APPROVED for p in ps)
            positive = [p.edge for p in ps if p.edge is not None and p.edge.net > 0]
            best = max((float(e.net) for e in positive), default=0.0)
            lines.append(f"{asset:<6}{tpl:<44}{len(ps):>6}{auto:>6}{user:>6}"
                         f"{len(ps) - auto - user:>6}{len(positive):>7}{best:>9.2f}")
    if run.refused:
        lines.append("\nrefused: " + ", ".join(
            f"{n} {why}" for why, n in run.refused.most_common(6)
        ))
    return lines
