from decimal import Decimal

import pytest
from rules_fixtures import RULES
from test_autoapprove import (
    BTC2_K,
    BTC2_P,
    BTC_K,
    BTC_P,
    ETH_K,
    ETH_P,
    MAX_K,
    MAX_P,
    T,
    market,
    shape,
    store_with,
    uncertain,
)

from moneymaker.matching import jev
from moneymaker.matching.autoapprove import AutoApprover
from moneymaker.matching.store import Routed
from moneymaker.matching.templates import (
    NO_AI_REASON,
    TemplateMatcher,
    find_matches,
    merge,
    target_series,
)
from moneymaker.models import MarketStatus, Venue
from moneymaker.orderbook import OrderBook


@pytest.fixture(autouse=True)
def no_ai(monkeypatch):
    """Makes any Jev call fail the test."""

    async def boom(*args, **kwargs):
        """Raises on any classification attempt."""
        raise AssertionError("Jev must not be called")

    monkeypatch.setattr(jev.JevClient, "classify", boom)


def approver_for(store) -> AutoApprover:
    """Returns the approver built from a store's user decisions."""
    return AutoApprover.from_store(store, T)


def ids(matches) -> set[tuple[str, str]]:
    """Returns the (kalshi, pm) id pairs of matches."""
    return {(m.kalshi.venue_market_id, m.pm.venue_market_id) for m in matches}


def test_pairs_btc_and_eth_markets_without_ai():
    """When new BTC and ETH markets share an approved template, it should pair each by asset."""
    store = store_with((BTC_K, BTC_P, "approved_equivalent", None))
    kalshi = [market(BTC_K), market(BTC2_K), market(ETH_K)]
    pm = [market(BTC2_P), market(ETH_P)]
    matches, _, in_templates = find_matches(kalshi, pm, approver_for(store))
    assert ids(matches) == {(BTC2_K, BTC2_P), (ETH_K, ETH_P)}
    assert in_templates == 2
    assert {m.asset for m in matches} == {"btc", "eth"}


def test_merge_stores_auto_approved_with_no_ai_reason():
    """When a match passes the approver, it should store it as auto_approved by template."""
    store = store_with((BTC_K, BTC_P, "approved_equivalent", None))
    approver = approver_for(store)
    matches, _, _ = find_matches([market(ETH_K)], [market(ETH_P)], approver)
    pair, reason, new = merge(store, approver, matches[0])
    assert pair is not None and reason is None and new
    row = store.get_match(ETH_K, ETH_P)
    assert (row["status"], row["polarity"], row["decided_by"]) == ("auto_approved", "same", "auto")
    assert NO_AI_REASON in row["reasons"]
    assert merge(store, approver, matches[0])[2] is False


def test_refuses_instants_one_minute_apart():
    """When settlement instants differ by one minute, it should not pair the markets."""
    store = store_with((BTC_K, BTC_P, "approved_equivalent", None))
    late = market(ETH_K, RULES[ETH_K].replace("9 PM EST on Nov 5", "9:01 PM EST on Nov 5"))
    matches, refused, _ = find_matches([late], [market(ETH_P)], approver_for(store))
    assert matches == []
    assert refused["no Kalshi market with the same asset and instants"] == 1
    d = approver_for(store).check(late, market(ETH_P), shape(late, market(ETH_P)),
                                  Routed("uncertain"), require_jev=False)
    assert "settlement times differ" in d.reasons


def test_keeps_yearly_max_template_refused():
    """When the yearly max template ends one minute before the PM market, it should refuse."""
    store = store_with((MAX_K, MAX_P, "approved_equivalent", None))
    matches, refused, _ = find_matches([market(MAX_K)], [market(MAX_P)], approver_for(store))
    assert matches == []
    assert sum(refused.values()) == 1


def test_refuses_different_assets():
    """When the PM market names another asset, it should not pair it."""
    store = store_with((BTC_K, BTC_P, "approved_equivalent", None))
    btc_rules = RULES[ETH_P].replace("Ethereum", "Bitcoin").replace("ETHUSD_RTI", "BRTI")
    btc_rules = btc_rules.replace("Ether-Dollar Real-Time Index", "Bitcoin Real-Time Index")
    p = market(ETH_P, btc_rules)
    matches, _, _ = find_matches([market(ETH_K)], [p], approver_for(store))
    assert matches == []
    d = approver_for(store).check(market(ETH_K), p, shape(market(ETH_K), p),
                                  Routed("uncertain"), require_jev=False)
    assert "assets differ" in d.reasons


def test_user_decisions_win():
    """When the user already decided a matched pair, it should keep that decision."""
    store = store_with(
        (BTC_K, BTC_P, "approved_equivalent", None),
        (ETH_K, ETH_P, "uncertain", None),
        (BTC2_K, BTC2_P, "rejected", "rule: bulk"),
    )
    approver = approver_for(store)
    kalshi, pm = [market(ETH_K), market(BTC2_K)], [market(ETH_P), market(BTC2_P)]
    matches, _, _ = find_matches(kalshi, pm, approver)
    assert len(matches) == 2
    for m in matches:
        pair, _, new = merge(store, approver, m)
        assert pair is not None and not new
    assert store.get_match(ETH_K, ETH_P)["status"] == "uncertain"
    assert store.get_match(BTC2_K, BTC2_P)["status"] == "rejected"
    assert store.get_match(ETH_K, ETH_P)["decided_by"] == "user"


def test_require_jev_toggles_only_score_checks():
    """When Jev scores are missing or disagree, it should refuse only with require_jev True."""
    store = store_with((BTC_K, BTC_P, "approved_equivalent", None))
    approver = approver_for(store)
    k, p = market(ETH_K), market(ETH_P)
    assert not approver.check(k, p, shape(k, p), Routed("uncertain")).approved
    assert approver.check(k, p, shape(k, p), Routed("uncertain"), require_jev=False).approved
    wrong = uncertain(polarity="inverted")
    assert not approver.check(k, p, shape(k, p), wrong).approved
    assert approver.check(k, p, shape(k, p), wrong, require_jev=False).approved
    late = market(ETH_K, RULES[ETH_K].replace("9 PM EST on Nov 5", "9:01 PM EST on Nov 5"))
    assert not approver.check(late, p, shape(late, p), wrong, require_jev=False).approved


class FakeKalshi:
    """A Kalshi client double serving fixture markets per series and a fixed book."""

    signer = None

    def __init__(self, markets):
        """Stores the markets and records requested series."""
        self.markets = markets
        self.requested: list[str] = []

    async def list_series(self):
        """Returns the series of the fixture markets plus one unrelated series."""
        tickers = {m.venue_market_id.split("-", 1)[0] for m in self.markets} | {"KXOTHER"}
        return [{"ticker": t, "category": "Crypto", "fee_multiplier": 1} for t in tickers]

    async def list_markets(self, *, series_ticker=None, limit=None):
        """Returns the fixture markets of one series."""
        self.requested.append(series_ticker)
        return [m for m in self.markets if m.venue_market_id.startswith(series_ticker + "-")]

    async def get_orderbook(self, market_id):
        """Returns a book whose YES asks at 0.40."""
        book = OrderBook(Venue.KALSHI, market_id)
        book.replace([(Decimal("0.38"), Decimal(100))], [(Decimal("0.40"), Decimal(100))])
        return book


class FakePM:
    """A Polymarket US client double without keys that serves fixture markets."""

    signer = None

    def __init__(self, markets):
        """Stores the markets."""
        self.markets = markets

    async def list_markets(self, *, categories=None, limit=None):
        """Returns the fixture markets."""
        return self.markets

    async def get_orderbook(self, market_id):
        """Returns a book whose YES bids at 0.50."""
        book = OrderBook(Venue.POLYMARKET_US, market_id)
        book.replace([(Decimal("0.50"), Decimal(100))], [(Decimal("0.52"), Decimal(100))])
        return book


def open_market(market_id: str):
    """Returns a fixture market marked open."""
    return market(market_id).model_copy(update={"status": MarketStatus.OPEN})


async def test_matcher_run_fetches_template_series_and_prices_pairs():
    """When run with known template series, it should fetch only those and price the pairs."""
    store = store_with((BTC_K, BTC_P, "approved_equivalent", None))
    store.db.execute("UPDATE matches SET inputs_hash = ?",
                     (jev.inputs_hash(open_market(BTC_K), open_market(BTC_P)),))
    assert target_series(store, set()) == {"KXBTCD"}
    kalshi = FakeKalshi([open_market(BTC_K), open_market(BTC2_K), open_market(ETH_K)])
    pm = FakePM([open_market(BTC_P), open_market(BTC2_P), open_market(ETH_P)])
    matcher = TemplateMatcher(store, "crypto", thresholds=T)
    run = await matcher.run(kalshi, pm)  # type: ignore[arg-type]
    assert "KXOTHER" not in kalshi.requested and "KXETHD" not in kalshi.requested
    got = {(p.match.kalshi.venue_market_id, p.status) for p in run.pairs}
    assert got == {(BTC_K, "approved_equivalent"), (BTC2_K, "auto_approved")}
    assert all(p.edge is not None and p.edge.net > 0 for p in run.pairs)
    n = store.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert n == 2
    run = await matcher.run(kalshi, pm, force_full=True)  # type: ignore[arg-type]
    assert "KXETHD" in kalshi.requested
    assert {p.match.kalshi.venue_market_id for p in run.pairs} == {BTC_K, BTC2_K, ETH_K}
    assert "KXETHD" in matcher.series


def test_merge_suspends_user_approval_when_rules_change():
    """When a user-approved pair's inputs changed, it should suspend it rather than approve."""
    store = store_with((BTC_K, BTC_P, "approved_equivalent", None))
    approver = approver_for(store)
    matches, _, _ = find_matches([market(BTC_K)], [market(BTC_P)], approver)
    pair, _, new = merge(store, approver, matches[0])
    assert pair is not None and pair.status == "suspended" and not new
    assert merge(store, approver, matches[0])[0].status == "suspended"


def test_user_implications_price_only_the_safe_direction():
    """When a user approved an implication, it should price only the broader-side direction."""
    from moneymaker.matching.candidates import Candidate
    from moneymaker.matching.extract import Check
    from moneymaker.matching.pipeline import legs_for

    k, p = market(ETH_K), market(ETH_P)
    kb, pb = OrderBook(Venue.KALSHI, ETH_K), OrderBook(Venue.POLYMARKET_US, ETH_P)
    routed = Routed("approved_implication", "implication", "same", "pm_broader")
    plan = legs_for("approved_implication", routed, Candidate(k, p, 1.0, Check("ok")), kb, pb)
    assert isinstance(plan, tuple) and plan[0] == "K-NO + PM-YES (implication)"
