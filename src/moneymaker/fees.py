from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal

from moneymaker.models import Market, Venue

CENT = Decimal("0.01")
KALSHI_TAKER_RATE = Decimal("0.07")
PM_US_DEFAULT_THETA = Decimal("0.0695")


def series_ticker(kalshi_ticker: str) -> str:
    """Returns the series prefix of a Kalshi ticker, e.g. KXBTCY for KXBTCY-27JAN0100-B22500."""
    return kalshi_ticker.split("-", 1)[0]


class FeeModel:
    """Computes taker fees per order for each venue.

    Kalshi: 0.07 x series multiplier x C x p x (1 - p), rounded up to the cent.
    Polymarket US: theta x C x p x (1 - p), banker's-rounded to the cent, theta from the market.
    """

    def __init__(self, kalshi_multipliers: dict[str, Decimal] | None = None) -> None:
        """Stores per-series Kalshi fee multipliers; unknown series use 1."""
        self.kalshi_multipliers = kalshi_multipliers or {}

    def rate(self, market: Market) -> Decimal:
        """Returns the fee coefficient applied to C x p x (1 - p) for market."""
        if market.venue is Venue.KALSHI:
            mult = self.kalshi_multipliers.get(series_ticker(market.venue_market_id), Decimal(1))
            return KALSHI_TAKER_RATE * mult
        return market.fee_coefficient if market.fee_coefficient is not None else PM_US_DEFAULT_THETA

    def unrounded(self, market: Market, price: Decimal, contracts: Decimal) -> Decimal:
        """Returns the exact fee for contracts at price before per-order rounding."""
        return self.rate(market) * contracts * price * (1 - price)

    def order_fee(self, market: Market, fills: Iterable[tuple[Decimal, Decimal]]) -> Decimal:
        """Returns the rounded fee for one order filled as (price, contracts) pieces."""
        exact = sum((self.unrounded(market, p, c) for p, c in fills), Decimal(0))
        rounding = ROUND_CEILING if market.venue is Venue.KALSHI else ROUND_HALF_EVEN
        return exact.quantize(CENT, rounding=rounding)
