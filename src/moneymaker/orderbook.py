from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from moneymaker.models import BookSnapshot, PriceLevel, Side, Venue

ONE = Decimal(1)
ZERO = Decimal(0)


class SequenceGap(Exception):
    def __init__(self, expected: int, got: int) -> None:
        super().__init__(f"sequence gap: expected {expected}, got {got}")
        self.expected = expected
        self.got = got


@dataclass(frozen=True)
class Fill:
    """Result of walking a book for a target size."""

    size: Decimal
    cost: Decimal
    worst_price: Decimal | None

    @property
    def avg_price(self) -> Decimal | None:
        return self.cost / self.size if self.size else None


@dataclass
class OrderBook:
    """Mutable L2 book for one binary market, stored in YES-perspective.

    yes_bids maps price -> size for resting bids on YES. yes_asks maps price -> size
    for resting offers to sell YES; a NO bid at p is a YES ask at 1 - p.
    """

    venue: Venue
    venue_market_id: str
    yes_bids: dict[Decimal, Decimal] = field(default_factory=dict)
    yes_asks: dict[Decimal, Decimal] = field(default_factory=dict)
    seq: int | None = None
    exchange_ts: datetime | None = None
    updated_ts: datetime | None = None
    synced: bool = False

    def invalidate(self) -> None:
        """Clears the book and marks it unsynced until the next full snapshot."""
        self.yes_bids.clear()
        self.yes_asks.clear()
        self.seq = None
        self.synced = False

    def replace(
        self,
        yes_bids: Iterable[tuple[Decimal, Decimal]],
        yes_asks: Iterable[tuple[Decimal, Decimal]],
        seq: int | None = None,
        exchange_ts: datetime | None = None,
    ) -> None:
        self.yes_bids = {p: s for p, s in yes_bids if s > 0}
        self.yes_asks = {p: s for p, s in yes_asks if s > 0}
        self.seq = seq
        self.synced = True
        self._touch(exchange_ts)

    def check_seq(self, seq: int | None) -> None:
        """Raises SequenceGap when seq does not directly follow the last applied seq."""
        if seq is None or self.seq is None:
            return
        if seq != self.seq + 1:
            raise SequenceGap(self.seq + 1, seq)

    def set_level(
        self,
        book_side: str,
        price: Decimal,
        size: Decimal,
        seq: int | None = None,
        exchange_ts: datetime | None = None,
    ) -> None:
        """Sets absolute size at a level of 'bid' or 'ask'; size 0 removes it."""
        self.check_seq(seq)
        levels = self._levels(book_side)
        if size <= 0:
            levels.pop(price, None)
        else:
            levels[price] = size
        if seq is not None:
            self.seq = seq
        self._touch(exchange_ts)

    def apply_delta(
        self,
        book_side: str,
        price: Decimal,
        delta: Decimal,
        seq: int | None = None,
        exchange_ts: datetime | None = None,
    ) -> None:
        """Adds delta contracts at a level of 'bid' or 'ask'; non-positive results remove it."""
        self.check_seq(seq)
        levels = self._levels(book_side)
        new_size = levels.get(price, ZERO) + delta
        if new_size <= 0:
            levels.pop(price, None)
        else:
            levels[price] = new_size
        if seq is not None:
            self.seq = seq
        self._touch(exchange_ts)

    def best_bid(self) -> tuple[Decimal, Decimal] | None:
        if not self.yes_bids:
            return None
        p = max(self.yes_bids)
        return p, self.yes_bids[p]

    def best_ask(self) -> tuple[Decimal, Decimal] | None:
        if not self.yes_asks:
            return None
        p = min(self.yes_asks)
        return p, self.yes_asks[p]

    def is_crossed(self) -> bool:
        bid, ask = self.best_bid(), self.best_ask()
        return bid is not None and ask is not None and bid[0] >= ask[0]

    def buy_levels(self, side: Side) -> list[tuple[Decimal, Decimal]]:
        """Returns the levels available to a buyer of side, best price first."""
        if side is Side.YES:
            return sorted(self.yes_asks.items())
        return sorted(((ONE - p, s) for p, s in self.yes_bids.items()))

    def walk(self, side: Side, size: Decimal, limit: Decimal | None = None) -> Fill:
        """Simulates buying up to size contracts of side, stopping at limit price if given."""
        remaining, cost, worst = size, ZERO, None
        for price, avail in self.buy_levels(side):
            if remaining <= 0 or (limit is not None and price > limit):
                break
            take = min(avail, remaining)
            cost += take * price
            remaining -= take
            worst = price
        return Fill(size=size - remaining, cost=cost, worst_price=worst)

    def snapshot(self, depth: int | None = None) -> BookSnapshot:
        bids = sorted(self.yes_bids.items(), reverse=True)[:depth]
        asks = sorted(self.yes_asks.items())[:depth]
        return BookSnapshot(
            venue=self.venue,
            venue_market_id=self.venue_market_id,
            yes_bids=tuple(PriceLevel(price=p, size=s) for p, s in bids),
            yes_asks=tuple(PriceLevel(price=p, size=s) for p, s in asks),
            exchange_ts=self.exchange_ts,
            received_ts=self.updated_ts or datetime.now(UTC),
            seq=self.seq,
        )

    def _levels(self, book_side: str) -> dict[Decimal, Decimal]:
        if book_side == "bid":
            return self.yes_bids
        if book_side == "ask":
            return self.yes_asks
        raise ValueError(f"book_side must be 'bid' or 'ask', got {book_side!r}")

    def _touch(self, exchange_ts: datetime | None) -> None:
        if exchange_ts is not None:
            self.exchange_ts = exchange_ts
        self.updated_ts = datetime.now(UTC)
