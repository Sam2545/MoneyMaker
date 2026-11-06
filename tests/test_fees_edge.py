from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from moneymaker.edge import best_edge, pair_legs, price_pair
from moneymaker.fees import FeeModel, series_ticker
from moneymaker.models import Market, Venue
from moneymaker.orderbook import OrderBook

SOON = datetime.now(UTC) + timedelta(days=36.5)


def kalshi_market(ticker: str = "KXBTCY-27JAN0100-B47500") -> Market:
    """Returns a Kalshi market settling in 36.5 days."""
    return Market(venue=Venue.KALSHI, venue_market_id=ticker, title="t", expiration_time=SOON)


def pm_market(slug: str = "cpc-btc-47500", theta: str = "0.0695") -> Market:
    """Returns a PM US market settling in 36.5 days."""
    return Market(
        venue=Venue.POLYMARKET_US, venue_market_id=slug, title="t",
        fee_coefficient=D(theta), close_time=SOON,
    )


def book(venue: Venue, bids, asks) -> OrderBook:
    """Returns a synced book with the given (price, size) levels."""
    b = OrderBook(venue, "x")
    b.replace([(D(p), D(s)) for p, s in bids], [(D(p), D(s)) for p, s in asks])
    return b


def test_series_ticker():
    """When given a Kalshi market ticker, it should return the series prefix."""
    assert series_ticker("KXBTCY-27JAN0100-B47500") == "KXBTCY"


def test_kalshi_fee_rounds_up_and_uses_multiplier():
    """When computing a Kalshi fee, it should apply the series multiplier and round up."""
    fees = FeeModel({"KXBTCY": D(0), "KXMLBGAME": D("0.5")})
    assert fees.order_fee(kalshi_market(), [(D("0.50"), D(100))]) == D("0.00")
    assert fees.order_fee(kalshi_market("KXFED-26DEC-T3.75"), [(D("0.50"), D(1))]) == D("0.02")
    assert fees.order_fee(kalshi_market("KXMLBGAME-X"), [(D("0.50"), D(100))]) == D("0.88")


def test_pm_fee_uses_bankers_rounding():
    """When computing a PM US fee, it should use theta and round half to even."""
    fees = FeeModel()
    assert fees.order_fee(pm_market(), [(D("0.50"), D(100))]) == D("1.74")
    assert fees.order_fee(pm_market(theta="0.1"), [(D("0.50"), D(1))]) == D("0.02")
    assert fees.order_fee(pm_market(), [(D("0.50"), D("2.8776"))]) == D("0.05")


def test_price_pair_sizes_to_marginal_edge():
    """When the second level is unprofitable, it should size only the profitable first level."""
    fees = FeeModel({"KXBTCY": D(0)})
    kb = book(Venue.KALSHI, [], [("0.40", "100"), ("0.45", "100")])
    pb = book(Venue.POLYMARKET_US, [("0.45", "1000")], [])
    (label, ka, pa), _ = pair_legs((kalshi_market(), kb), (pm_market(), pb), "same")
    edge = price_pair(ka, pa, fees, label=label)
    assert edge.contracts == D(50)
    assert edge.cost == D(50) * D("0.40") + D(50) * D("0.55")
    assert edge.net == D(50) - edge.cost - edge.fees
    assert edge.net > 0
    assert edge.top_marginal > D("0.03")


def test_price_pair_reports_negative_top_when_unprofitable():
    """When even the best prices lose after fees, it should size zero and report the loss."""
    fees = FeeModel()
    kb = book(Venue.KALSHI, [], [("0.50", "100")])
    pb = book(Venue.POLYMARKET_US, [("0.51", "100")], [])
    (label, ka, pa), _ = pair_legs(
        (kalshi_market("KXFED-X"), kb), (pm_market(), pb), "same"
    )
    edge = price_pair(ka, pa, fees, label=label)
    assert edge.contracts == 0
    assert edge.top_marginal < 0


def test_inverted_polarity_buys_same_side_on_both():
    """When polarity is inverted, it should pair YES with YES and NO with NO."""
    labels = [label for label, *_ in pair_legs(
        (kalshi_market(), book(Venue.KALSHI, [], [])),
        (pm_market(), book(Venue.POLYMARKET_US, [], [])),
        "inverted",
    )]
    assert labels == ["K-YES + PM-YES", "K-NO + PM-NO"]


def test_best_edge_picks_profitable_direction_and_annualizes():
    """When only one direction is profitable, it should return it with an annualized return."""
    fees = FeeModel({"KXBTCY": D(0)})
    kb = book(Venue.KALSHI, [("0.30", "200")], [("0.31", "200")])
    pb = book(Venue.POLYMARKET_US, [("0.36", "200")], [("0.37", "200")])
    edge = best_edge((kalshi_market(), kb), (pm_market(), pb), "same", fees)
    assert edge.label == "K-YES + PM-NO"
    assert edge.contracts == D(100)
    assert edge.annualized is not None and edge.annualized > 0
