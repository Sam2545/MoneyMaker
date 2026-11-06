from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from moneymaker.fees import FeeModel
from moneymaker.models import Market, Side
from moneymaker.orderbook import OrderBook

ONE = Decimal(1)
ZERO = Decimal(0)


@dataclass(frozen=True)
class Leg:
    """One side of a pair trade: buy `side` on `market` using `book`."""

    market: Market
    book: OrderBook
    side: Side


@dataclass(frozen=True)
class Edge:
    """The sized, fee-adjusted result of buying both legs of a pair."""

    label: str
    contracts: Decimal
    cost: Decimal
    fees: Decimal
    net: Decimal
    days_to_settle: Decimal | None
    top_marginal: Decimal | None
    fills_a: tuple[tuple[Decimal, Decimal], ...]
    fills_b: tuple[tuple[Decimal, Decimal], ...]

    @property
    def net_per_contract(self) -> Decimal:
        """Returns net profit divided by contracts, or 0 when nothing is sized."""
        return self.net / self.contracts if self.contracts else ZERO

    @property
    def capital(self) -> Decimal:
        """Returns the cash required to open both legs including fees."""
        return self.cost + self.fees

    @property
    def annualized(self) -> Decimal | None:
        """Returns net / capital scaled to a 365-day year, or None without a settle date."""
        if not self.contracts or not self.capital or not self.days_to_settle:
            return None
        return self.net / self.capital * Decimal(365) / self.days_to_settle


def days_until(*markets: Market, now: datetime | None = None) -> Decimal | None:
    """Returns days until the latest expiration (or close) among markets, at least 1/24."""
    now = now or datetime.now(UTC)
    times = [m.expiration_time or m.close_time for m in markets]
    if any(t is None for t in times):
        return None
    seconds = max((t - now).total_seconds() for t in times)  # type: ignore[operator]
    return max(Decimal(seconds) / Decimal(86400), Decimal(1) / Decimal(24))


def price_pair(
    a: Leg,
    b: Leg,
    fees: FeeModel,
    *,
    min_edge_per_contract: Decimal = Decimal("0.002"),
    max_contracts: Decimal | None = None,
    depth_haircut: Decimal = Decimal("0.5"),
    label: str = "",
) -> Edge:
    """Sizes a two-leg buy that pays $1 per pair and returns its fee-adjusted edge.

    Walks both ladders together and stops before the first marginal contract whose
    pre-rounding edge is below min_edge_per_contract. Each visible level is used up to
    depth_haircut of its size. Fees are then rounded per leg as one order each.
    """
    ladder_a = [(p, s * depth_haircut) for p, s in a.book.buy_levels(a.side)]
    ladder_b = [(p, s * depth_haircut) for p, s in b.book.buy_levels(b.side)]
    rate_a, rate_b = fees.rate(a.market), fees.rate(b.market)
    fills_a: list[tuple[Decimal, Decimal]] = []
    fills_b: list[tuple[Decimal, Decimal]] = []
    total = ZERO
    i = j = 0
    rem_a = ladder_a[0][1] if ladder_a else ZERO
    rem_b = ladder_b[0][1] if ladder_b else ZERO
    top_marginal = None
    if ladder_a and ladder_b:
        pa, pb = ladder_a[0][0], ladder_b[0][0]
        top_marginal = ONE - pa - pb - rate_a * pa * (ONE - pa) - rate_b * pb * (ONE - pb)
    while i < len(ladder_a) and j < len(ladder_b):
        pa, pb = ladder_a[i][0], ladder_b[j][0]
        marginal = ONE - pa - pb - rate_a * pa * (ONE - pa) - rate_b * pb * (ONE - pb)
        if marginal < min_edge_per_contract:
            break
        take = min(rem_a, rem_b)
        if max_contracts is not None:
            take = min(take, max_contracts - total)
        if take <= 0:
            break
        fills_a.append((pa, take))
        fills_b.append((pb, take))
        total += take
        rem_a -= take
        rem_b -= take
        if rem_a <= 0:
            i += 1
            rem_a = ladder_a[i][1] if i < len(ladder_a) else ZERO
        if rem_b <= 0:
            j += 1
            rem_b = ladder_b[j][1] if j < len(ladder_b) else ZERO

    cost = sum((p * c for p, c in fills_a + fills_b), ZERO)
    fee_total = (fees.order_fee(a.market, fills_a) if fills_a else ZERO) + (
        fees.order_fee(b.market, fills_b) if fills_b else ZERO
    )
    return Edge(
        label=label,
        contracts=total,
        cost=cost,
        fees=fee_total,
        net=total - cost - fee_total,
        days_to_settle=days_until(a.market, b.market),
        top_marginal=top_marginal,
        fills_a=tuple(fills_a),
        fills_b=tuple(fills_b),
    )


def pair_legs(
    kalshi: tuple[Market, OrderBook],
    pm: tuple[Market, OrderBook],
    polarity: str,
) -> list[tuple[str, Leg, Leg]]:
    """Returns both trade directions for a pair as (label, kalshi_leg, pm_leg).

    polarity "same" means Kalshi YES pays when PM YES pays; "inverted" means Kalshi YES
    pays when PM NO pays.
    """
    (km, kb), (pm_m, pb) = kalshi, pm
    if polarity == "same":
        return [
            ("K-YES + PM-NO", Leg(km, kb, Side.YES), Leg(pm_m, pb, Side.NO)),
            ("K-NO + PM-YES", Leg(km, kb, Side.NO), Leg(pm_m, pb, Side.YES)),
        ]
    if polarity == "inverted":
        return [
            ("K-YES + PM-YES", Leg(km, kb, Side.YES), Leg(pm_m, pb, Side.YES)),
            ("K-NO + PM-NO", Leg(km, kb, Side.NO), Leg(pm_m, pb, Side.NO)),
        ]
    raise ValueError(f"polarity must be 'same' or 'inverted', got {polarity!r}")


def best_edge(
    kalshi: tuple[Market, OrderBook],
    pm: tuple[Market, OrderBook],
    polarity: str,
    fees: FeeModel,
    **kwargs,
) -> Edge:
    """Returns the better of the two directions, by net dollars then top-of-book marginal edge."""
    edges = [
        price_pair(ka, pa, fees, label=label, **kwargs)
        for label, ka, pa in pair_legs(kalshi, pm, polarity)
    ]
    return max(edges, key=lambda e: (e.net, e.top_marginal if e.top_marginal is not None else -ONE))
