from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from moneymaker.models import Market, Venue
from moneymaker.orderbook import OrderBook


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
