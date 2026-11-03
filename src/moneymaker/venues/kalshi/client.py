from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog
import websockets

from moneymaker.backoff import backoff_delay
from moneymaker.config import Settings, get_settings
from moneymaker.models import Market, MarketStatus, Venue
from moneymaker.orderbook import ONE, OrderBook
from moneymaker.ratelimit import TokenBucket
from moneymaker.venues.kalshi.auth import KalshiSigner

log = structlog.get_logger(__name__)

PAGE_SIZE = 1000

_STATUS = {
    "active": MarketStatus.OPEN,
    "initialized": MarketStatus.CLOSED,
    "inactive": MarketStatus.CLOSED,
    "closed": MarketStatus.CLOSED,
    "disputed": MarketStatus.CLOSED,
    "determined": MarketStatus.SETTLED,
    "amended": MarketStatus.SETTLED,
    "finalized": MarketStatus.SETTLED,
}


class KalshiSeqGap(Exception):
    pass


def _ts(v: str | None) -> datetime | None:
    """Parses an ISO-8601 timestamp, returning None for empty values."""
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None


def _levels(raw: list[list[str]] | None) -> list[tuple[Decimal, Decimal]]:
    """Converts Kalshi [price, quantity] string pairs to Decimal tuples."""
    return [(Decimal(p), Decimal(q)) for p, q in raw or []]


def parse_market(m: dict[str, Any]) -> Market:
    """Converts a Kalshi market payload to a normalized Market."""
    title = m.get("title") or ""
    yes_label = m.get("yes_sub_title") or None
    full_title = f"{title}: {yes_label}" if yes_label and yes_label not in title else title
    rules = "\n\n".join(x for x in (m.get("rules_primary"), m.get("rules_secondary")) if x)
    steps = [Decimal(r["step"]) for r in m.get("price_ranges") or [] if r.get("step")]
    return Market(
        venue=Venue.KALSHI,
        venue_market_id=m["ticker"],
        event_id=m.get("event_ticker"),
        title=full_title,
        yes_label=yes_label,
        rules=rules,
        close_time=_ts(m.get("close_time")),
        expiration_time=_ts(m.get("latest_expiration_time") or m.get("expiration_time")),
        status=_STATUS.get(m.get("status", ""), MarketStatus.UNKNOWN),
        tick_size=min(steps) if steps else Decimal("0.01"),
        url=f"https://kalshi.com/markets/{m.get('event_ticker', m['ticker'])}",
        raw=m,
    )


def load_bids(book: OrderBook, yes: list | None, no: list | None, **kw: Any) -> None:
    """Loads Kalshi bid-only ladders into a YES-perspective book (NO bid p -> YES ask 1 - p)."""
    book.replace(
        yes_bids=_levels(yes),
        yes_asks=[(ONE - p, q) for p, q in _levels(no)],
        **kw,
    )


def apply_ws_delta(book: OrderBook, msg: dict[str, Any]) -> None:
    """Applies an orderbook_delta message to a YES-perspective book.
    NO-side deltas update the YES ask at 1 - price.
    """
    price, delta = Decimal(msg["price_dollars"]), Decimal(msg["delta_fp"])
    ts = datetime.fromtimestamp(msg["ts_ms"] / 1000, UTC) if msg.get("ts_ms") else None
    if msg["side"] == "yes":
        book.apply_delta("bid", price, delta, exchange_ts=ts)
    else:
        book.apply_delta("ask", ONE - price, delta, exchange_ts=ts)


class KalshiClient:
    venue = Venue.KALSHI

    def __init__(self, settings: Settings | None = None) -> None:
        """Creates the HTTP client and rate limiter, plus a signer when key settings are present."""
        self.settings = settings or get_settings()
        s = self.settings
        self._http = httpx.AsyncClient(base_url=s.kalshi_rest_url, timeout=s.http_timeout_s)
        self._rest_path = urlparse(s.kalshi_rest_url).path
        self._limiter = TokenBucket(rate=15, capacity=30)
        self.signer = (
            KalshiSigner.from_pem_file(s.kalshi_key_id, s.kalshi_private_key_path)
            if s.kalshi_key_id and s.kalshi_private_key_path
            else None
        )

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Sends a rate-limited GET and returns the JSON body.
        Retries 429 and 5xx responses with backoff, up to 5 times.
        """
        headers = self.signer.headers("GET", self._rest_path + path) if self.signer else {}
        for attempt in itertools.count():
            await self._limiter.acquire()
            resp = await self._http.get(path, params=params, headers=headers)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= 5:
                    resp.raise_for_status()
                await asyncio.sleep(max(1.0, backoff_delay(attempt)))
                if self.signer:
                    headers = self.signer.headers("GET", self._rest_path + path)
                continue
            resp.raise_for_status()
            return resp.json()
        raise AssertionError("unreachable")

    async def list_markets(self, *, limit: int | None = None) -> list[Market]:
        """Returns open binary markets, following the cursor until exhausted or limit is reached."""
        out: list[Market] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"status": "open", "limit": PAGE_SIZE, "mve_filter": "exclude"}
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/markets", params)
            out.extend(
                parse_market(m)
                for m in data.get("markets") or []
                if m.get("market_type", "binary") == "binary"
            )
            cursor = data.get("cursor")
            if not cursor or (limit is not None and len(out) >= limit):
                break
        return out[:limit] if limit is not None else out

    async def get_orderbook(self, venue_market_id: str) -> OrderBook:
        """Returns a full-depth REST snapshot of a market's book."""
        data = await self._get(f"/markets/{venue_market_id}/orderbook", {"depth": 0})
        ob = data.get("orderbook_fp") or {}
        book = OrderBook(self.venue, venue_market_id)
        load_bids(book, ob.get("yes_dollars"), ob.get("no_dollars"))
        return book

    def stream_orderbooks(self, venue_market_ids: Sequence[str]) -> AsyncIterator[OrderBook]:
        """Returns the WebSocket book stream.
        Raises RuntimeError when no Kalshi keys are configured.
        """
        if self.signer is None:
            raise RuntimeError(
                "Kalshi WebSocket requires KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH"
            )
        return self._stream_ws(venue_market_ids)

    async def _stream_ws(self, venue_market_ids: Sequence[str]) -> AsyncIterator[OrderBook]:
        """Subscribes to orderbook_delta and yields each book on change.
        Reconnects with backoff on disconnect or sequence gap.
        """
        assert self.signer is not None
        books = {t: OrderBook(self.venue, t) for t in venue_market_ids}
        ws_url = self.settings.kalshi_ws_url
        ws_path = urlparse(ws_url).path
        attempt = 0
        while True:
            last_seq: dict[int, int] = {}
            try:
                async with websockets.connect(
                    ws_url,
                    additional_headers=self.signer.headers("GET", ws_path),
                    ping_interval=self.settings.ws_ping_interval_s,
                    max_size=None,
                ) as ws:
                    await ws.send(json.dumps({
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {"channels": ["orderbook_delta"], "market_tickers": list(books)},
                    }))
                    async for raw in ws:
                        frame = json.loads(raw)
                        kind = frame.get("type")
                        if kind == "error":
                            log.error("kalshi.ws_error", frame=frame)
                            continue
                        if kind not in ("orderbook_snapshot", "orderbook_delta"):
                            continue
                        sid, seq = frame.get("sid"), frame.get("seq")
                        if sid is not None and seq is not None:
                            prev = last_seq.get(sid)
                            if prev is not None and seq != prev + 1:
                                raise KalshiSeqGap(f"sid {sid}: expected {prev + 1}, got {seq}")
                            last_seq[sid] = seq
                        msg = frame["msg"]
                        book = books.get(msg.get("market_ticker", ""))
                        if book is None:
                            continue
                        if kind == "orderbook_snapshot":
                            load_bids(book, msg.get("yes_dollars_fp"), msg.get("no_dollars_fp"))
                        elif book.synced:
                            apply_ws_delta(book, msg)
                        else:
                            continue
                        attempt = 0
                        yield book
            except (OSError, TimeoutError, websockets.WebSocketException, KalshiSeqGap) as exc:
                delay = backoff_delay(attempt, cap=self.settings.reconnect_max_backoff_s)
                log.warning("kalshi.ws_reconnect", error=repr(exc), delay=round(delay, 2))
                for book in books.values():
                    book.invalidate()
                attempt += 1
                await asyncio.sleep(delay)

    async def aclose(self) -> None:
        """Closes the underlying HTTP client."""
        await self._http.aclose()
