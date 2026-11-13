from datetime import UTC, datetime, timedelta

from rules_fixtures import RULES

from moneymaker.matching.autoapprove import (
    AutoApprover,
    rules_assets,
    rules_fingerprint,
    settlement_instants,
)
from moneymaker.matching.candidates import Candidate
from moneymaker.matching.extract import Check, compare, describe
from moneymaker.matching.jev import Answers
from moneymaker.matching.pipeline import auto_approve, legs_for
from moneymaker.matching.routing import Thresholds, route
from moneymaker.matching.store import MatchStore, Routed
from moneymaker.models import Market, Venue

T = Thresholds.load()
BTC_K, BTC_P = "KXBTCD-26NOV0600-T75299.99", "cpc-btc-above-hr-2026-11-06-0500z-75300"
BTC2_K, BTC2_P = "KXBTCD-26NOV0523-T75899.99", "cpc-btc-above-hr-2026-11-06-0400z-75900"
ETH_K, ETH_P = "KXETHD-26NOV0521-T2099.99", "cpceth-eth-above-hr-2026-11-06-0200z-2100"
MAX_K, MAX_P = "KXBTCMAXY-26DEC31-149999.99", "cpc-btc-hitprice-high-yr-12-31-2026-150k"
TITLES = {
    BTC_K: ("Bitcoin price on Nov 6, 2026?", "$75,300 or above"),
    BTC_P: ("BTC Price at 12:00AM ET on Fri, Nov 6", "$75,300 or above"),
    BTC2_K: ("Bitcoin price on Nov 5, 2026?", "$75,900 or above"),
    BTC2_P: ("BTC Price at 11:00PM ET on Thu, Nov 5", "$75,900 or above"),
    ETH_K: ("Ethereum price at Nov 5, 2026 at 9pm EST?", "$2,100 or above"),
    ETH_P: ("ETH Price at 9:00PM ET on Thu, Nov 5", "$2,100 or above"),
    MAX_K: ("Will Bitcoin be above $149,999.99 by Dec 31, 2026?", "Above $149,999.99"),
    MAX_P: ("How high will Bitcoin get this year?", "Above $149,999.99"),
}


def market(market_id: str, rules: str | None = None) -> Market:
    """Returns a market with the real rules and title of market_id."""
    venue = Venue.KALSHI if market_id.startswith("KX") else Venue.POLYMARKET_US
    title, yes = TITLES.get(market_id, (market_id, None))
    return Market(venue=venue, venue_market_id=market_id, title=title, yes_label=yes,
                  rules=RULES[market_id] if rules is None else rules)


def utc(*args: int) -> datetime:
    """Returns a UTC datetime."""
    return datetime(*args, tzinfo=UTC)


def answers(**over) -> Answers:
    """Returns Jev answers that pass every approve gate, with overrides."""
    raw = {
        "same_event": {"type": "noul", "noul": 0.99},
        "same_event_swapped": {"type": "noul", "noul": 0.99},
        "polarity": {"type": "choice", "choice": "same", "confidence": 0.99,
                     "probabilities": {"same": 0.995, "inverted": 0.004, "neither": 0.001}},
        "polarity_swapped": {"type": "choice", "choice": "same", "confidence": 0.99,
                             "probabilities": {"same": 0.99, "inverted": 0.005, "neither": 0.005}},
        "same_source": {"type": "noul", "noul": 0.97},
        "deadline": {"type": "choice", "choice": "identical", "confidence": 0.98,
                     "probabilities": {"identical": 0.98, "A_window_longer": 0.01,
                                       "B_window_longer": 0.005, "incompatible": 0.005}},
        "divergence": {"type": "noul", "noul": 0.01},
        "divergence_kind": {"type": "choice", "choice": "none", "confidence": 0.8,
                            "probabilities": {"none": 0.9, "postponement": 0.1}},
    }
    raw.update(over)
    return Answers("jev-1.14.0", raw)


def uncertain(same_event: float = 0.6, polarity: str = "same", p: float = 0.97) -> Routed:
    """Returns an uncertain routing whose Jev scores carry the given values."""
    probs = {"same": 0.01, "inverted": 0.01, "neither": 0.01, polarity: p}
    r = route(answers(
        same_event={"type": "noul", "noul": same_event},
        same_event_swapped={"type": "noul", "noul": same_event},
        polarity={"type": "choice", "choice": polarity, "confidence": p, "probabilities": probs},
        polarity_swapped={"type": "choice", "choice": polarity, "confidence": p,
                          "probabilities": probs},
    ), Check("ok"), T)
    assert r.status == "uncertain"
    return r


def store_with(*decisions: tuple[str, str, str, str | None], same_event: float = 0.7) -> MatchStore:
    """Returns an in-memory store holding the given (kalshi, pm, status, note) user decisions."""
    store = MatchStore(":memory:")
    for kid, pid, status, note in decisions:
        store.put_markets([market(kid), market(pid)])
        store.upsert_routed(kid, pid, "h", 0.9, uncertain(same_event))
        polarity = "same" if status == "approved_equivalent" else None
        relation = "equivalent" if polarity else None
        store.record_decision(kid, pid, status, relation=relation, polarity=polarity, note=note)
    return store


def shape(k: Market, p: Market) -> Check:
    """Returns the shape check of a pair."""
    return compare(describe(k), describe(p))


def test_fingerprint_is_equal_across_hourly_instances():
    """When two BTC hourly markets differ only in hour and strike, it should match."""
    assert rules_fingerprint(RULES[BTC_K]) == rules_fingerprint(RULES[BTC2_K])
    assert rules_fingerprint(RULES[BTC_P]) == rules_fingerprint(RULES[BTC2_P])


def test_fingerprint_is_equal_across_assets():
    """When a BTC and an ETH market come from one template, it should give one fingerprint."""
    assert rules_fingerprint(RULES[BTC_K]) == rules_fingerprint(RULES[ETH_K])
    assert rules_fingerprint(RULES[BTC_P]) == rules_fingerprint(RULES[ETH_P])


def test_fingerprint_keeps_different_wording_apart():
    """When the rules use different wording, it should give different fingerprints."""
    assert rules_fingerprint(RULES[BTC_P]) != rules_fingerprint(
        RULES[BTC_P].replace("at or above", "above")
    )
    assert rules_fingerprint(RULES[BTC_K]) != rules_fingerprint(RULES[BTC_P])


def test_fingerprint_masks_numbers_times_and_names():
    """When text has prices, clock times, dates and assets, it should mask each token."""
    fp = rules_fingerprint("Bitcoin at or above $75,300.50 at 5:00PM ET on Fri, Nov 6, 2026")
    assert fp == "<asset> at or above # at # <tz> on <weekday>, <month> #, #"


def test_instants_parse_both_orders_and_formats():
    """When rules name a time and a date in either order, it should return each as UTC."""
    assert settlement_instants("at 5 PM EST on Nov 6, 2026") == {utc(2026, 11, 6, 22)}
    assert settlement_instants("5:00PM ET on Nov 6, 2026") == {utc(2026, 11, 6, 22)}
    assert settlement_instants("before Jan 1, 2027 at 12:00am ET") == {utc(2027, 1, 1, 5)}
    assert settlement_instants("until 12:00 AM ET on January 1, 2027") == {utc(2027, 1, 1, 5)}
    assert settlement_instants("at 5:00PM ET on Fri, Nov 6, 2026") == {utc(2026, 11, 6, 22)}
    assert settlement_instants("at 12 PM EDT on Jul 4, 2026") == {utc(2026, 7, 4, 16)}


def test_instants_ignore_times_without_dates():
    """When a time has no date next to it, it should not return an instant."""
    assert settlement_instants("before 12 AM EST is above 5") == frozenset()


def test_instants_match_on_real_hourly_weekly_and_15m_rules():
    """When real Kalshi and PM rules settle at the same moment, it should return equal sets."""
    pairs = [
        (BTC_K, BTC_P, {utc(2026, 11, 6, 5)}),
        ("KXBTC-26NOV0617-B67250", "cpc-btc-range-wk-2026-11-06-67000", {utc(2026, 11, 6, 22)}),
        ("KXBTC15M-26NOV052330-30", "cpc-btc-updown-15m-2026-11-06-0415z",
         {utc(2026, 11, 6, 4, 15), utc(2026, 11, 6, 4, 30)}),
    ]
    for k, p, expected in pairs:
        assert settlement_instants(RULES[k]) == expected
        assert settlement_instants(RULES[p]) == expected


def test_instants_find_the_one_minute_gap_in_yearly_max_rules():
    """When Kalshi ends at 11:59 PM and PM at midnight, it should return instants a minute apart."""
    (k,) = settlement_instants(RULES[MAX_K])
    (p,) = settlement_instants(RULES[MAX_P])
    assert p - k == timedelta(minutes=1)


def test_assets_come_from_names_and_index_codes():
    """When rules name an asset or its index code, it should return the canonical asset."""
    assert rules_assets(RULES[ETH_K]) == {"eth"}
    assert rules_assets(RULES[ETH_P]) == {"eth"}
    assert rules_assets(RULES[BTC_P]) == {"btc"}
    assert rules_assets("CF Benchmarks' BNBUSD_RTI") == {"bnb"}


def test_eth_pair_inherits_an_approved_btc_template():
    """When an ETH pair matches an approved BTC template and all checks pass, it should approve."""
    store = store_with((BTC_K, BTC_P, "approved_equivalent", None))
    approver = AutoApprover.from_store(store, T)
    k, p = market(ETH_K), market(ETH_P)
    d = approver.check(k, p, shape(k, p), uncertain())
    assert d.approved, d.reasons
    assert d.polarity == "same"
    assert BTC_K in d.reasons[0]


def test_refuses_when_instants_differ_by_one_minute():
    """When the settlement times differ by one minute, it should refuse."""
    store = store_with((MAX_K, MAX_P, "approved_equivalent", None))
    approver = AutoApprover.from_store(store, T)
    k, p = market(MAX_K), market(MAX_P)
    d = approver.check(k, p, shape(k, p), uncertain())
    assert not d.approved
    assert "settlement times differ" in d.reasons


def test_refuses_when_template_is_conflicted():
    """When the user also rejected a pair of the same template, it should refuse."""
    store = store_with(
        (BTC_K, BTC_P, "approved_equivalent", None),
        (BTC2_K, BTC2_P, "rejected", "not the same"),
    )
    approver = AutoApprover.from_store(store, T)
    k, p = market(ETH_K), market(ETH_P)
    d = approver.check(k, p, shape(k, p), uncertain())
    assert not d.approved
    assert any("conflicted" in r for r in d.reasons)


def test_ignores_rule_rejections_when_building_templates():
    """When a rejection's note starts with 'rule:', it should not conflict the template."""
    store = store_with(
        (BTC_K, BTC_P, "approved_equivalent", None),
        (BTC2_K, BTC2_P, "rejected", "rule: bulk"),
    )
    approver = AutoApprover.from_store(store, T)
    k, p = market(ETH_K), market(ETH_P)
    assert approver.check(k, p, shape(k, p), uncertain()).approved


def test_refuses_when_jev_polarity_disagrees():
    """When Jev's polarity differs from the approved polarity, it should refuse."""
    store = store_with((BTC_K, BTC_P, "approved_equivalent", None))
    approver = AutoApprover.from_store(store, T)
    k, p = market(ETH_K), market(ETH_P)
    d = approver.check(k, p, shape(k, p), uncertain(polarity="inverted"))
    assert not d.approved
    assert any("polarity inverted" in r for r in d.reasons)


def test_refuses_when_scores_are_missing():
    """When the routing carries no Jev scores, it should refuse."""
    store = store_with((BTC_K, BTC_P, "approved_equivalent", None))
    approver = AutoApprover.from_store(store, T)
    k, p = market(ETH_K), market(ETH_P)
    d = approver.check(k, p, shape(k, p), Routed("uncertain"))
    assert not d.approved
    assert "Jev scores missing" in d.reasons


def test_refuses_low_same_event_and_implication_and_mixed_assets():
    """When same_event is far below the template, the shape is an implication, or assets
    differ, it should refuse."""
    store = store_with((BTC_K, BTC_P, "approved_equivalent", None), same_event=0.8)
    approver = AutoApprover.from_store(store, T)
    k, p = market(ETH_K), market(ETH_P)
    assert not approver.check(k, p, shape(k, p), uncertain(same_event=0.5)).approved
    assert approver.check(k, p, shape(k, p), uncertain(same_event=0.66)).approved
    implication = Check("implication", broader="kalshi")
    assert not approver.check(k, p, implication, uncertain()).approved
    btc_p = market(BTC_P, RULES[ETH_P].replace("Ethereum", "Bitcoin"))
    assert "assets differ" in approver.check(k, btc_p, Check("ok"), uncertain()).reasons


def test_pipeline_stores_auto_approved_and_prices_it():
    """When an undecided pair passes, it should store auto_approved and price it by polarity."""
    store = store_with((BTC_K, BTC_P, "approved_equivalent", None))
    approver = AutoApprover.from_store(store, T)
    k, p = market(ETH_K), market(ETH_P)
    c = Candidate(k, p, 0.9, shape(k, p))
    routed = auto_approve(approver, store, c, uncertain())
    assert (routed.status, routed.relation, routed.polarity) == (
        "auto_approved", "equivalent", "same"
    )
    assert store.upsert_routed(ETH_K, ETH_P, "h2", 0.9, routed) == "auto_approved"
    assert store.get_match(ETH_K, ETH_P)["decided_by"] == "auto"
    assert legs_for("auto_approved", routed, c, None, None) == "same"  # type: ignore[arg-type]
    assert [r["kalshi_id"] for r in store.review_queue(["auto_approved"])] == [ETH_K]


def test_pipeline_keeps_user_decisions():
    """When the pair already has a user decision, it should not auto-approve it."""
    store = store_with(
        (BTC_K, BTC_P, "approved_equivalent", None),
        (ETH_K, ETH_P, "uncertain", None),
    )
    approver = AutoApprover.from_store(store, T)
    k, p = market(ETH_K), market(ETH_P)
    c = Candidate(k, p, 0.9, shape(k, p))
    routed = auto_approve(approver, store, c, uncertain())
    assert routed.status == "uncertain"
    assert store.upsert_routed(ETH_K, ETH_P, "h", 0.9, routed) == "uncertain"
    assert store.get_match(ETH_K, ETH_P)["decided_by"] == "user"
