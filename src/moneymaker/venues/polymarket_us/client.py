from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog
import websockets

from moneymaker.backoff import backoff_delay
from moneymaker.config import Settings, get_settings
from moneymaker.models import Market, MarketStatus, Venue
from moneymaker.orderbook import OrderBook
from moneymaker.ratelimit import TokenBucket
from moneymaker.venues.polymarket_us.auth import PolymarketUSSigner

log = structlog.get_logger(__name__)

GATEWAY_URL = "https://gateway.polymarket.us"
WS_URL = "wss://api.polymarket.us/v1/ws/markets"
WS_PATH = "/v1/ws/markets"
PAGE_SIZE = 100
SUBSCRIBE_CHUNK = 50
HEARTBEAT_TIMEOUT_S = 30.0

_STATUS = {
    "MARKET_STATUS_OPEN": MarketStatus.OPEN,
    "MARKET_STATUS_CLOSED": MarketStatus.CLOSED,
    "MARKET_STATUS_HALTED": MarketStatus.CLOSED,
    "MARKET_STATUS_RESOLVING": MarketStatus.CLOSED,
    "MARKET_STATUS_RESOLVED": MarketStatus.SETTLED,
}


def _dec(v: Any) -> Decimal:
    """Converts a number, string, or {value: ...} amount to Decimal."""
    if isinstance(v, dict):
        v = v.get("value")
    return Decimal(str(v))


def _ts(v: str | None) -> datetime | None:
    """Parses an ISO-8601 timestamp, returning None for empty values."""
    if not v:
        return None
    # Trims nanosecond precision to microseconds for datetime.fromisoformat.
    if "." in v:
        head, frac = v.rstrip("Z").split(".", 1)
        v = f"{head}.{frac[:6]}+00:00"
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


GENERIC_SIDES = {"yes", "no", "up", "down", "over", "under"}


def side_labels(m: dict[str, Any]) -> tuple[str | None, str | None]:
    """Returns the named (YES, NO) outcomes from marketSides, or (None, None) for Yes/No sides."""
    sides = m.get("marketSides") or []
    long = next((s.get("description") for s in sides if s.get("long")), None)
    short = next((s.get("description") for s in sides if s.get("long") is False), None)
    if not long or long.strip().lower() in GENERIC_SIDES:
        return None, None
    return long, short


def parse_market(m: dict[str, Any]) -> Market:
    """Converts a Polymarket US market payload to a normalized Market.

    The YES label is the named long side (e.g. a team or player) when marketSides names one,
    otherwise the market's title field (e.g. a price range).
    """
    rules = "\n\n".join(x for x in (m.get("description"), m.get("rulesDisclaimer")) if x)
    question, title = m.get("question") or "", m.get("title") or ""
    full_title = f"{question}: {title}" if question and title and question != title else (
        question or title
    )
    tick = m.get("orderPriceMinTickSize")
    fee = m.get("feeCoefficient")
    named_yes, named_no = side_labels(m)
    return Market(
        venue=Venue.POLYMARKET_US,
        venue_market_id=m["slug"],
        event_id=m.get("eventSlug") or m.get("seriesSlug"),
        title=full_title,
        yes_label=named_yes or title or None,
        no_label=named_no,
        rules=rules,
        close_time=_ts(m.get("endDate")),
        expiration_time=_ts(m.get("endDate")),
        status=_STATUS.get(m.get("status", ""), MarketStatus.UNKNOWN),
        tick_size=Decimal(str(tick)) if tick is not None else Decimal("0.01"),
        fee_coefficient=Decimal(str(fee)) if fee is not None else None,
        url=f"https://polymarket.us/market/{m['slug']}",
        raw=m,
    )


def apply_market_data(book: OrderBook, md: dict[str, Any]) -> None:
    """Replaces book contents with a marketData payload (REST /book or WS full snapshot)."""
    book.replace(
        yes_bids=[(_dec(lvl["px"]), _dec(lvl["qty"])) for lvl in md.get("bids") or []],
        yes_asks=[(_dec(lvl["px"]), _dec(lvl["qty"])) for lvl in md.get("offers") or []],
        exchange_ts=_ts(md.get("transactTime")),
    )


class PolymarketUSClient:
    venue = Venue.POLYMARKET_US

    def __init__(self, settings: Settings | None = None, *, rate_per_s: float = 5.0) -> None:
        """Creates the gateway HTTP client, rate limiter, and a signer when keys are set."""
        self.settings = settings or get_settings()
        self._http = httpx.AsyncClient(base_url=GATEWAY_URL, timeout=self.settings.http_timeout_s)
        self._limiter = TokenBucket(rate=rate_per_s, capacity=rate_per_s)
        s = self.settings
        self.signer = None
        if s.polymarket_us_key_id and s.polymarket_us_secret_key:
            secret = s.polymarket_us_secret_key.get_secret_value()
            self.signer = PolymarketUSSigner(s.polymarket_us_key_id, secret)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Sends a rate-limited GET to the public gateway and returns the JSON body.
        Retries 429 and 5xx responses with backoff, up to 5 times.
        """
        for attempt in itertools.count():
            await self._limiter.acquire()
            resp = await self._http.get(path, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= 5:
                    resp.raise_for_status()
                await asyncio.sleep(max(1.0, backoff_delay(attempt)))
                continue
            resp.raise_for_status()
            return resp.json()
        raise AssertionError("unreachable")

    async def list_markets(
        self, *, limit: int | None = None, categories: Sequence[str] | None = None
    ) -> list[Market]:
        """Returns active, unclosed markets, optionally only in categories, paging by offset.

        Stops at a short page or when limit is reached.
        """
        out: list[Market] = []
        offset = 0
        while True:
            params: dict[str, Any] = {
                "active": "true", "closed": "false", "limit": PAGE_SIZE, "offset": offset,
            }
            if categories:
                params["categories[]"] = list(categories)
            data = await self._get("/v1/markets", params)
            page = data.get("markets") or []
            out.extend(parse_market(m) for m in page if m.get("slug"))
            if len(page) < PAGE_SIZE or (limit is not None and len(out) >= limit):
                break
            offset += PAGE_SIZE
        return out[:limit] if limit is not None else out

    async def get_orderbook(self, venue_market_id: str) -> OrderBook:
        """Returns a REST snapshot of a market's book."""
        data = await self._get(f"/v1/markets/{venue_market_id}/book")
        book = OrderBook(self.venue, venue_market_id)
        apply_market_data(book, data.get("marketData") or {})
        return book

    def stream_orderbooks(self, venue_market_ids: Sequence[str]) -> AsyncIterator[OrderBook]:
        """Returns the WebSocket book stream when keys are configured, otherwise REST polling."""
        if self.signer is None:
            log.warning("polymarket_us.no_keys_polling_rest")
            return self.poll_orderbooks(venue_market_ids)
        return self._stream_ws(venue_market_ids)

    async def poll_orderbooks(
        self, venue_market_ids: Sequence[str], interval_s: float = 2.0
    ) -> AsyncIterator[OrderBook]:
        """Yields REST snapshots for each market in a loop, sharing the REST rate limit."""
        while True:
            started = asyncio.get_running_loop().time()
            for slug in venue_market_ids:
                try:
                    yield await self.get_orderbook(slug)
                except httpx.HTTPError as exc:
                    log.warning("polymarket_us.poll_error", slug=slug, error=str(exc))
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.0, interval_s - elapsed))

    async def _stream_ws(self, venue_market_ids: Sequence[str]) -> AsyncIterator[OrderBook]:
        """Subscribes to market data and yields each book on every full snapshot.
        Reconnects with backoff on disconnect or heartbeat timeout.
        """
        assert self.signer is not None
        books = {slug: OrderBook(self.venue, slug) for slug in venue_market_ids}
        slugs = list(venue_market_ids)
        attempt = 0
        while True:
            try:
                async with websockets.connect(
                    WS_URL,
                    additional_headers=self.signer.headers("GET", WS_PATH),
                    ping_interval=self.settings.ws_ping_interval_s,
                    max_size=None,
                ) as ws:
                    for i in range(0, len(slugs), SUBSCRIBE_CHUNK):
                        await ws.send(json.dumps({"subscribe": {
                            "requestId": f"md-{i}",
                            "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                            "marketSlugs": slugs[i : i + SUBSCRIBE_CHUNK],
                        }}))
                    log.info("polymarket_us.ws_subscribed", markets=len(slugs))
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=HEARTBEAT_TIMEOUT_S)
                        attempt = 0
                        msg = json.loads(raw)
                        if "error" in msg:
                            log.error("polymarket_us.ws_error", msg=msg)
                            continue
                        md = msg.get("marketData")
                        if not md:
                            continue
                        book = books.get(md.get("marketSlug", ""))
                        if book is None:
                            continue
                        apply_market_data(book, md)
                        yield book
            except (OSError, TimeoutError, websockets.WebSocketException) as exc:
                delay = backoff_delay(attempt, cap=self.settings.reconnect_max_backoff_s)
                log.warning("polymarket_us.ws_reconnect", error=repr(exc), delay=round(delay, 2))
                for book in books.values():
                    book.invalidate()
                attempt += 1
                await asyncio.sleep(delay)

    async def aclose(self) -> None:
        """Closes the underlying HTTP client."""
        await self._http.aclose()
