from decimal import Decimal as D

import pytest

from moneymaker.models import Side, Venue
from moneymaker.orderbook import OrderBook, SequenceGap


def make_book() -> OrderBook:
    """Returns a two-level-per-side book at seq 1 for the tests below."""
    book = OrderBook(Venue.KALSHI, "TEST")
    book.replace(
        yes_bids=[(D("0.40"), D(100)), (D("0.38"), D(50))],
        yes_asks=[(D("0.43"), D(30)), (D("0.45"), D(200))],
        seq=1,
    )
    return book


def test_best_levels():
    """When the book has bids and asks, it should report the best of each and not be crossed."""
    book = make_book()
    assert book.best_bid() == (D("0.40"), D(100))
    assert book.best_ask() == (D("0.43"), D(30))
    assert not book.is_crossed()


def test_walk_yes_across_levels():
    """When buying YES past the best ask's size, it should fill across levels."""
    fill = make_book().walk(Side.YES, D(50))
    assert fill.size == D(50)
    assert fill.cost == D(30) * D("0.43") + D(20) * D("0.45")
    assert fill.worst_price == D("0.45")


def test_walk_no_uses_inverted_bids():
    """When buying NO, it should fill against YES bids at 1 - bid price."""
    fill = make_book().walk(Side.NO, D(120))
    assert fill.size == D(120)
    assert fill.cost == D(100) * D("0.60") + D(20) * D("0.62")


def test_walk_respects_limit_and_depth():
    """When a limit price or the book's depth is reached, it should stop filling there."""
    book = make_book()
    assert book.walk(Side.YES, D(100), limit=D("0.44")).size == D(30)
    assert book.walk(Side.YES, D(1000)).size == D(230)


def test_delta_and_removal():
    """When a delta empties a level or sets a new one, it should update the best prices and seq."""
    book = make_book()
    book.apply_delta("bid", D("0.40"), D(-100), seq=2)
    assert book.best_bid() == (D("0.38"), D(50))
    book.set_level("ask", D("0.42"), D(5), seq=3)
    assert book.best_ask() == (D("0.42"), D(5))
    assert book.seq == 3


def test_sequence_gap_raises():
    """When a delta skips a sequence number, it should raise SequenceGap."""
    book = make_book()
    with pytest.raises(SequenceGap):
        book.apply_delta("bid", D("0.40"), D(1), seq=5)


def test_snapshot_ordering():
    """When snapshotted, it should order bids high-to-low and asks low-to-high."""
    snap = make_book().snapshot()
    assert [lvl.price for lvl in snap.yes_bids] == [D("0.40"), D("0.38")]
    assert [lvl.price for lvl in snap.yes_asks] == [D("0.43"), D("0.45")]
