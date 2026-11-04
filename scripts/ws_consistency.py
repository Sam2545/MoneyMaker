"""Checks that Kalshi books built from WebSocket deltas match fresh REST snapshots.

Usage: uv run python scripts/ws_consistency.py
"""

import asyncio

from moneymaker.orderbook import OrderBook
from moneymaker.venues.kalshi.client import KalshiClient

STREAM_S = 15
MARKETS = 5
ATTEMPTS = 5


def books_equal(ws: OrderBook, rest: OrderBook) -> bool:
    """Returns True when both books hold identical bid and ask levels."""
    return ws.yes_bids == rest.yes_bids and ws.yes_asks == rest.yes_asks


def diff_levels(ws: OrderBook, rest: OrderBook) -> str:
    """Returns the bid and ask levels that differ between the WS and REST books."""
    out = []
    for side, a, b in (("bid", ws.yes_bids, rest.yes_bids), ("ask", ws.yes_asks, rest.yes_asks)):
        for price in sorted(set(a) | set(b)):
            if a.get(price) != b.get(price):
                out.append(f"{side} {price}: ws={a.get(price)} rest={b.get(price)}")
    return "; ".join(out[:5])


async def check(client: KalshiClient, ticker: str, books: dict[str, OrderBook]) -> str:
    """Compares the streamed book with REST up to ATTEMPTS times and reports the outcome.

    The stream keeps running between attempts, so a book that converges is reported as a match.
    """
    rest = None
    for attempt in range(1, ATTEMPTS + 1):
        rest = await client.get_orderbook(ticker)
        ws = books.get(ticker)
        if ws is None:
            return f"{ticker}: no WS book"
        if books_equal(ws, rest):
            return f"{ticker:<48} MATCH (attempt {attempt})"
        await asyncio.sleep(1)
    return f"{ticker:<48} MISMATCH after {ATTEMPTS} attempts: {diff_levels(books[ticker], rest)}"


async def main() -> None:
    """Streams the busiest Kalshi markets, then compares each streamed book with its REST book."""
    client = KalshiClient()
    markets = await client.list_markets(limit=3000)
    markets.sort(key=lambda m: float(m.raw.get("volume_24h_fp") or 0), reverse=True)
    ids = [m.venue_market_id for m in markets[:MARKETS]]
    books: dict[str, OrderBook] = {}
    updates = 0

    async def consume() -> None:
        """Keeps the latest streamed book per ticker and counts updates."""
        nonlocal updates
        async for book in client.stream_orderbooks(ids):
            books[book.venue_market_id] = book
            updates += 1

    task = asyncio.create_task(consume())
    await asyncio.sleep(STREAM_S)
    for t in ids:
        print(await check(client, t, books), flush=True)
    task.cancel()
    print(f"{updates} WS updates applied")
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
