from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Collection, Sequence
from typing import Any, Protocol

import structlog

from moneymaker.models import Market, Venue
from moneymaker.orderbook import OrderBook

log = structlog.get_logger(__name__)
_DONE = object()


class VenueClient(Protocol):
    """Read-only market data interface every venue adapter implements."""

    venue: Venue

    async def list_markets(self, *, limit: int | None = None) -> list[Market]:
        """Returns open binary markets, normalized."""
        ...

    async def get_orderbook(self, venue_market_id: str) -> OrderBook:
        """Returns a REST snapshot of one market's book."""
        ...

    def stream_orderbooks(self, venue_market_ids: Sequence[str]) -> AsyncIterator[OrderBook]:
        """Yields the updated book each time a subscribed market changes.

        Reconnects and resyncs from a fresh snapshot on disconnect or sequence gap.
        """
        ...

    async def aclose(self) -> None:
        """Closes the underlying HTTP client."""
        ...


async def merge_streams(streams: Sequence[AsyncIterator[Any]]) -> AsyncIterator[Any]:
    """Yields items from several async iterators as they arrive, re-raising the first error.

    Every source is closed when the merged stream is closed.
    """
    if len(streams) == 1:
        async for item in streams[0]:
            yield item
        return
    queue: asyncio.Queue[Any] = asyncio.Queue()

    async def pump(source: AsyncIterator[Any]) -> None:
        """Copies one source into the queue, then marks it done."""
        try:
            async for item in source:
                await queue.put(item)
        except Exception as exc:
            await queue.put(exc)
        finally:
            await queue.put(_DONE)

    tasks = [asyncio.create_task(pump(s)) for s in streams]
    try:
        remaining = len(tasks)
        while remaining:
            item = await queue.get()
            if item is _DONE:
                remaining -= 1
            elif isinstance(item, Exception):
                raise item
            else:
                yield item
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for s in streams:
            with contextlib.suppress(Exception):
                await s.aclose()  # type: ignore[attr-defined]


async def collect_books(
    stream: AsyncIterator[OrderBook], wanted: Collection[str], timeout_s: float, venue: str
) -> dict[str, OrderBook]:
    """Returns the first synced book of each wanted market from a stream within timeout_s.

    The stream is closed afterwards; markets without a book in time are left out.
    """
    wanted = set(wanted)
    got: dict[str, OrderBook] = {}
    try:
        async with asyncio.timeout(timeout_s):
            async for book in stream:
                if book.synced and book.venue_market_id in wanted:
                    got[book.venue_market_id] = book
                    if len(got) == len(wanted):
                        break
    except TimeoutError:
        log.warning("venue.snapshot_timeout", venue=venue, got=len(got), wanted=len(wanted))
    finally:
        await stream.aclose()  # type: ignore[attr-defined]
    return got
