"""Streams live books from the busiest markets on each venue and reports how many synced.

Usage: uv run python scripts/ws_smoke.py [kalshi] [polymarket_us]
"""

import asyncio
import sys
from decimal import Decimal, InvalidOperation

from moneymaker.cli import fmt_top
from moneymaker.models import Market, Venue
from moneymaker.venues.base import VenueClient
from moneymaker.venues.kalshi.client import KalshiClient
from moneymaker.venues.polymarket_us.client import PolymarketUSClient

DURATION_S = 20
MARKETS_PER_VENUE = 5


def volume_24h(market: Market) -> Decimal:
    """Returns the market's 24h volume from its raw payload, or 0 when absent or malformed."""
    try:
        return Decimal(str(market.raw.get("volume_24h_fp") or 0))
    except InvalidOperation:
        return Decimal(0)


async def busiest(client: VenueClient, n: int) -> list[str]:
    """Returns the ids of up to n open markets, ordered by 24h volume on Kalshi."""
    markets = await client.list_markets(limit=3000 if client.venue is Venue.KALSHI else 400)
    if client.venue is Venue.KALSHI:
        markets.sort(key=volume_24h, reverse=True)
    return [m.venue_market_id for m in markets[:n]]


async def run(client: VenueClient) -> None:
    """Streams the busiest markets for DURATION_S seconds and prints each first book and totals."""
    ids = await busiest(client, MARKETS_PER_VENUE)
    print(f"[{client.venue}] streaming {len(ids)} markets for {DURATION_S}s: {ids}", flush=True)
    updates, synced = 0, set()

    async def consume() -> None:
        """Counts updates and prints top-of-book the first time each market syncs."""
        nonlocal updates
        async for book in client.stream_orderbooks(ids):
            updates += 1
            if book.venue_market_id not in synced:
                synced.add(book.venue_market_id)
                print(f"[{client.venue}] first book: {fmt_top(book)}", flush=True)

    try:
        await asyncio.wait_for(consume(), DURATION_S)
    except TimeoutError:
        pass
    finally:
        await client.aclose()
    print(
        f"[{client.venue}] RESULT: {updates} updates, {len(synced)}/{len(ids)} books synced",
        flush=True,
    )


async def main() -> None:
    """Runs the smoke test concurrently for each venue named on the command line."""
    factories = {"kalshi": KalshiClient, "polymarket_us": PolymarketUSClient}
    which = sys.argv[1:] or list(factories)
    await asyncio.gather(*(run(factories[w]()) for w in which))


if __name__ == "__main__":
    asyncio.run(main())
