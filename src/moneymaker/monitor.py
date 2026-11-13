from __future__ import annotations

import asyncio
import contextlib
import signal
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import structlog

from moneymaker.config import Settings, get_settings
from moneymaker.edge import Edge
from moneymaker.fees import FeeModel
from moneymaker.matching.autoapprove import rules_assets
from moneymaker.matching.review import template as series_template
from moneymaker.matching.store import MatchStore, Routed
from moneymaker.matching.templates import TemplateMatcher, TemplateRun, price_edge
from moneymaker.models import Market
from moneymaker.opportunities import (
    MERGE_GAP_S,
    MIN_SECONDS,
    Episode,
    EpisodeTracker,
    iso,
    local_day_bounds,
    render_text,
    summarize,
    write_daily,
)
from moneymaker.orderbook import OrderBook
from moneymaker.venues.kalshi.client import KalshiClient
from moneymaker.venues.polymarket_us.client import PolymarketUSClient

log = structlog.get_logger(__name__)

MONITORED = ("approved_equivalent", "auto_approved", "approved_implication")
HEARTBEAT_S = 60.0
TICK_S = 1.0
STREAM_RETRY_S = 5.0

BookKey = tuple[str, str]


@dataclass(frozen=True)
class MonitoredPair:
    """An approved pair being watched: both markets, its stored routing, asset and template."""

    kalshi: Market
    pm: Market
    status: str
    routed: Routed
    asset: str
    template: str

    @property
    def key(self) -> tuple[str, str]:
        """Returns the (kalshi id, pm id) key of the pair."""
        return self.kalshi.venue_market_id, self.pm.venue_market_id

    def closes_by(self, now: datetime) -> bool:
        """Returns True when either market's close time is at or before now."""
        return any(
            m.close_time is not None and m.close_time <= now for m in (self.kalshi, self.pm)
        )


def load_pairs(store: MatchStore, run: TemplateRun, now: datetime) -> list[MonitoredPair]:
    """Returns monitored-status pairs whose markets are both open in the run and not closed."""
    rows = store.db.execute(
        f"SELECT * FROM matches WHERE status IN ({','.join('?' * len(MONITORED))})", MONITORED
    )
    out = []
    for row in rows:
        k = run.markets.get(("kalshi", row["kalshi_id"]))
        p = run.markets.get(("pm", row["pm_id"]))
        if k is None or p is None:
            continue
        routed = Routed(row["status"], row["relation"], row["polarity"], row["direction"])
        pair = MonitoredPair(
            k, p, row["status"], routed, ",".join(sorted(rules_assets(k.rules))) or "-",
            series_template(row["kalshi_id"], row["pm_id"]),
        )
        if not pair.closes_by(now):
            out.append(pair)
    return out


StreamFactory = Callable[[Sequence[str]], AsyncIterator[OrderBook]]


class Monitor:
    """Streams both legs of every approved pair and records fee-adjusted opportunity episodes."""

    def __init__(
        self,
        store: MatchStore,
        fee_model: FeeModel | None = None,
        *,
        min_edge_per_contract: Decimal = Decimal("0.002"),
        log_dir: Path = Path("data/logs"),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Stores the store, fee model, pricing threshold, summary directory and clock."""
        self.store = store
        self.fee_model = fee_model or FeeModel()
        self.min_edge_per_contract = min_edge_per_contract
        self.log_dir = log_dir
        self.clock = clock
        self.pairs: dict[tuple[str, str], MonitoredPair] = {}
        self.by_book: dict[BookKey, list[MonitoredPair]] = defaultdict(list)
        self.books: dict[BookKey, OrderBook] = {}
        self.tracker = EpisodeTracker(self._store_episode)
        self.updates = 0
        self.evaluations = 0
        self.errors = 0

    def _store_episode(self, episode: Episode) -> None:
        """Writes a closed episode to the opportunities table and logs it."""
        row = episode.row()
        self.store.add_opportunity(row)
        log.info("monitor.episode", pair=f"{row['kalshi_id']} / {row['pm_id']}",
                 label=row["label"], duration_s=round(row["duration_s"], 2),
                 max_net=round(row["max_net"], 2), max_contracts=row["max_contracts"],
                 max_cents=round(row["max_cents_per_contract"], 2))

    def set_pairs(self, pairs: Sequence[MonitoredPair]) -> tuple[set[str], set[str]]:
        """Replaces the watched pairs, closing episodes of dropped ones; returns needed ids.

        Returns the Kalshi tickers and PM slugs whose books the pairs need.
        """
        now = self.clock()
        new = {p.key: p for p in pairs}
        for key in set(self.pairs) - set(new):
            self.tracker.close(key, now)
        self.pairs = new
        self.by_book = defaultdict(list)
        for p in pairs:
            self.by_book[("kalshi", p.kalshi.venue_market_id)].append(p)
            self.by_book[("pm", p.pm.venue_market_id)].append(p)
        for key in set(self.books) - set(self.by_book):
            del self.books[key]
        return ({p.kalshi.venue_market_id for p in pairs}, {p.pm.venue_market_id for p in pairs})

    def on_book(self, venue: str, book: OrderBook) -> None:
        """Records a book update and re-evaluates every pair that uses the book."""
        key = (venue, book.venue_market_id)
        self.updates += 1
        self.books[key] = book
        for pair in self.by_book.get(key, ()):
            self.evaluate(pair)

    def evaluate(self, pair: MonitoredPair) -> Edge | None:
        """Recomputes a pair's edge from its synced books and feeds it to the episode tracker.

        Errors are logged and close the pair's episode.
        """
        now = self.clock()
        kb = self.books.get(("kalshi", pair.kalshi.venue_market_id))
        pb = self.books.get(("pm", pair.pm.venue_market_id))
        edge = None
        if kb is not None and pb is not None and kb.synced and pb.synced:
            if not pair.closes_by(now):
                try:
                    edge = price_edge(pair.status, pair.routed, pair.kalshi, pair.pm, kb, pb,
                                      self.fee_model, self.min_edge_per_contract)
                except Exception as exc:
                    self.errors += 1
                    log.warning("monitor.price_error", pair=pair.key, error=repr(exc)[:200])
            self.evaluations += 1
        self.tracker.update(pair.key, edge, now, asset=pair.asset, template=pair.template)
        return edge

    def sweep(self) -> None:
        """Closes open episodes whose pair is gone, has closed, or lost a synced book."""
        now = self.clock()
        for key in list(self.tracker.open):
            pair = self.pairs.get(key)
            kb = self.books.get(("kalshi", key[0]))
            pb = self.books.get(("pm", key[1]))
            if (
                pair is None or pair.closes_by(now) or kb is None or pb is None
                or not kb.synced or not pb.synced
            ):
                self.tracker.close(key, now)

    def synced_books(self) -> int:
        """Returns how many tracked books are currently synced."""
        return sum(b.synced for b in self.books.values())

    def write_summary(self, at: datetime) -> tuple[Path, Path]:
        """Writes the daily JSON and Markdown summary for at's local date."""
        start, end = local_day_bounds(at)
        rows = self.store.opportunities_between(iso(start), iso(end))
        summary = summarize(rows, start=iso(start), end=iso(end))
        return write_daily(summary, self.log_dir, start.astimezone().date().isoformat())


BookCallback = Callable[[str, OrderBook], None]


async def consume(
    venue: str, factory: StreamFactory, ids: Sequence[str], on_book: BookCallback
) -> None:
    """Feeds every book from a venue stream to on_book, restarting the stream if it fails."""
    while True:
        stream = factory(list(ids))
        try:
            async for book in stream:
                on_book(venue, book)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("monitor.stream_failed", venue=venue, error=repr(exc)[:200])
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()  # type: ignore[attr-defined]
        await asyncio.sleep(STREAM_RETRY_S)


class Streams:
    """Holds one consumer task per venue and restarts it when its market set changes."""

    def __init__(self, factories: dict[str, StreamFactory], on_book: BookCallback) -> None:
        """Stores the per-venue stream factories and the book callback."""
        self.factories = factories
        self.on_book = on_book
        self.ids: dict[str, frozenset[str]] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    async def set(self, venue: str, ids: set[str]) -> bool:
        """Resubscribes a venue when its ids changed; returns True when it restarted."""
        frozen = frozenset(ids)
        if self.ids.get(venue) == frozen and venue in self.tasks:
            return False
        await self.stop(venue)
        self.ids[venue] = frozen
        if frozen:
            self.tasks[venue] = asyncio.create_task(
                consume(venue, self.factories[venue], sorted(frozen), self.on_book)
            )
        return True

    async def stop(self, venue: str) -> None:
        """Cancels a venue's consumer task, if any."""
        task = self.tasks.pop(venue, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def stop_all(self) -> None:
        """Cancels every consumer task."""
        for venue in list(self.tasks):
            await self.stop(venue)


async def run_monitor(
    scope: str,
    *,
    include: str | None = None,
    refresh_min: float = 5.0,
    duration_s: float | None = None,
    settings: Settings | None = None,
    min_edge_per_contract: Decimal = Decimal("0.002"),
    log_dir: Path = Path("data/logs"),
) -> Monitor:
    """Runs the template matcher every refresh_min minutes and monitors approved pairs live.

    Stops after duration_s seconds when given, otherwise runs until cancelled or SIGTERM.
    """
    settings = settings or get_settings()
    store = MatchStore(settings.match_db_path)
    kalshi, pm = KalshiClient(settings), PolymarketUSClient(settings)
    if kalshi.signer is None:
        raise RuntimeError("monitor requires Kalshi keys for the WebSocket book stream")
    matcher = TemplateMatcher(store, scope, include=include)
    monitor = Monitor(store, min_edge_per_contract=min_edge_per_contract, log_dir=log_dir)
    streams = Streams(
        {"kalshi": kalshi.stream_orderbooks, "pm": pm.stream_orderbooks}, monitor.on_book
    )
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    with contextlib.suppress(NotImplementedError, RuntimeError):
        loop.add_signal_handler(signal.SIGTERM, stop.set)
    started = loop.time()
    next_refresh = next_beat = started
    last_updates = 0
    last_tick = datetime.now(UTC)
    refreshing: asyncio.Task | None = None

    async def refresh() -> None:
        """Runs the template matcher, resubscribes changed streams and rewrites today's summary."""
        try:
            run = await matcher.run(kalshi, pm, price=False,
                                    min_edge_per_contract=min_edge_per_contract)
            monitor.fee_model = run.fee_model
            pairs = load_pairs(store, run, datetime.now(UTC))
            k_ids, p_ids = monitor.set_pairs(pairs)
            restarted = [v for v, ids in (("kalshi", k_ids), ("pm", p_ids))
                         if await streams.set(v, ids)]
            log.info("monitor.pairs", pairs=len(pairs), kalshi=len(k_ids), pm=len(p_ids),
                     resubscribed=restarted, matcher_s=round(run.elapsed_s, 1))
        except Exception as exc:
            log.warning("monitor.refresh_failed", error=repr(exc)[:300])
        try:
            monitor.write_summary(datetime.now(UTC))
        except OSError as exc:
            log.warning("monitor.summary_failed", error=repr(exc))

    try:
        while not stop.is_set() and (duration_s is None or loop.time() - started < duration_s):
            now = loop.time()
            if now >= next_refresh and (refreshing is None or refreshing.done()):
                refreshing = asyncio.create_task(refresh())
                next_refresh = now + refresh_min * 60
            monitor.sweep()
            tick = datetime.now(UTC)
            if tick.astimezone().date() != last_tick.astimezone().date():
                with contextlib.suppress(OSError):
                    monitor.write_summary(last_tick)
            last_tick = tick
            if now >= next_beat:
                log.info("monitor.heartbeat", pairs=len(monitor.pairs),
                         books_synced=monitor.synced_books(), books=len(monitor.books),
                         open_episodes=len(monitor.tracker.open),
                         episodes_closed=monitor.tracker.closed,
                         updates=monitor.updates - last_updates, errors=monitor.errors)
                last_updates = monitor.updates
                next_beat = now + HEARTBEAT_S
            await asyncio.sleep(TICK_S)
    finally:
        log.info("monitor.stopping", episodes_closed=monitor.tracker.closed,
                 open_episodes=len(monitor.tracker.open))
        if refreshing is not None and not refreshing.done():
            refreshing.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await refreshing
        await streams.stop_all()
        monitor.tracker.close_all(datetime.now(UTC))
        with contextlib.suppress(OSError):
            monitor.write_summary(datetime.now(UTC))
        await kalshi.aclose()
        await pm.aclose()
        store.close()
    return monitor


def opportunities_report(
    store: MatchStore, hours: float, *, merge_gap_s: float = MERGE_GAP_S,
    min_seconds: float = MIN_SECONDS, show_all: bool = False,
) -> str:
    """Returns the plain-text summary of episodes that started in the last hours."""
    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    rows = store.opportunities_between(iso(start), iso(end + timedelta(seconds=1)))
    summary = summarize(
        rows, start=iso(start), end=iso(end), merge_gap_s=merge_gap_s,
        min_seconds=0.0 if show_all else min_seconds,
    )
    return render_text(summary, show_all=show_all)
