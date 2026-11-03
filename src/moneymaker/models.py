from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Venue(StrEnum):
    KALSHI = "kalshi"
    POLYMARKET_US = "polymarket_us"


class MarketStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    SETTLED = "settled"
    UNKNOWN = "unknown"


class Side(StrEnum):
    YES = "yes"
    NO = "no"


class Market(BaseModel):
    """A binary market normalized across venues. Prices are dollars in [0, 1]."""

    model_config = ConfigDict(frozen=True)

    venue: Venue
    venue_market_id: str
    event_id: str | None = None
    title: str
    yes_label: str | None = None
    rules: str = ""
    close_time: datetime | None = None
    expiration_time: datetime | None = None
    status: MarketStatus = MarketStatus.UNKNOWN
    tick_size: Decimal = Decimal("0.01")
    fee_coefficient: Decimal | None = None
    url: str | None = None
    raw: dict = Field(default_factory=dict, repr=False, exclude=True)

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.venue_market_id}"


class PriceLevel(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: Decimal
    size: Decimal


class BookSnapshot(BaseModel):
    """Point-in-time YES-perspective book: bids and asks for the YES contract."""

    model_config = ConfigDict(frozen=True)

    venue: Venue
    venue_market_id: str
    yes_bids: tuple[PriceLevel, ...]
    yes_asks: tuple[PriceLevel, ...]
    exchange_ts: datetime | None = None
    received_ts: datetime
    seq: int | None = None

    @property
    def best_yes_bid(self) -> PriceLevel | None:
        return self.yes_bids[0] if self.yes_bids else None

    @property
    def best_yes_ask(self) -> PriceLevel | None:
        return self.yes_asks[0] if self.yes_asks else None
