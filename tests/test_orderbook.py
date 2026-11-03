from decimal import Decimal as D

import pytest

from moneymaker.models import Side, Venue
from moneymaker.orderbook import OrderBook, SequenceGap


def make_book() -> OrderBook:
    book = OrderBook(Venue.KALSHI, "TEST")
    book.replace(
        yes_bids=[(D("0.40"), D(100)), (D("0.38"), D(50))],
        yes_asks=[(D("0.43"), D(30)), (D("0.45"), D(200))],
        seq=1,
    )
    return book


def test_best_levels():
    book = make_book()
    assert book.best_bid() == (D("0.40"), D(100))
    assert book.best_ask() == (D("0.43"), D(30))
    assert not book.is_crossed()


def test_walk_yes_across_levels():
    fill = make_book().walk(Side.YES, D(50))
    assert fill.size == D(50)
    assert fill.cost == D(30) * D("0.43") + D(20) * D("0.45")
    assert fill.worst_price == D("0.45")


def test_walk_no_uses_inverted_bids():
    fill = make_book().walk(Side.NO, D(120))
    assert fill.size == D(120)
    assert fill.cost == D(100) * D("0.60") + D(20) * D("0.62")


def test_walk_respects_limit_and_depth():
    book = make_book()
    assert book.walk(Side.YES, D(100), limit=D("0.44")).size == D(30)
    assert book.walk(Side.YES, D(1000)).size == D(230)


def test_delta_and_removal():
    book = make_book()
    book.apply_delta("bid", D("0.40"), D(-100), seq=2)
    assert book.best_bid() == (D("0.38"), D(50))
    book.set_level("ask", D("0.42"), D(5), seq=3)
    assert book.best_ask() == (D("0.42"), D(5))
    assert book.seq == 3


def test_sequence_gap_raises():
    book = make_book()
    with pytest.raises(SequenceGap):
        book.apply_delta("bid", D("0.40"), D(1), seq=5)


def test_snapshot_ordering():
    snap = make_book().snapshot()
    assert [lvl.price for lvl in snap.yes_bids] == [D("0.40"), D("0.38")]
    assert [lvl.price for lvl in snap.yes_asks] == [D("0.43"), D("0.45")]
