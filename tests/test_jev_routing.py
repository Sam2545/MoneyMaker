import json

import httpx
import respx

from moneymaker.matching.extract import Check
from moneymaker.matching.jev import API_URL, Answers, JevClient, build_questions, inputs_hash
from moneymaker.matching.routing import Thresholds, route
from moneymaker.models import Market, Venue


def km(title: str, yes: str | None = None, ticker: str = "K1", rules: str = "") -> Market:
    """Returns a Kalshi market with the given title and YES label."""
    return Market(venue=Venue.KALSHI, venue_market_id=ticker, title=title, yes_label=yes,
                  rules=rules)


def pm(title: str, yes: str | None = None, slug: str = "P1", rules: str = "") -> Market:
    """Returns a PM US market with the given title and YES label."""
    return Market(venue=Venue.POLYMARKET_US, venue_market_id=slug, title=title, yes_label=yes,
                  rules=rules)


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


T = Thresholds.load()


def test_route_approves_only_when_every_gate_passes():
    """When all gates pass, it should route to pending_review as an equivalent."""
    r = route(answers(), Check("ok"), T)
    assert (r.status, r.relation, r.polarity) == ("pending_review", "equivalent", "same")


def test_route_rejects_and_holds():
    """When same_event is low it should reject; when a gate misses it should be uncertain."""
    low = answers(same_event={"type": "noul", "noul": 0.1})
    assert route(low, Check("ok"), T).status == "rejected"
    diverges = answers(divergence={"type": "noul", "noul": 0.2})
    r = route(diverges, Check("ok"), T)
    assert r.status == "uncertain" and any("divergence" in x for x in r.reasons)


def test_route_swap_disagreement_is_uncertain():
    """When swapped-order polarity disagrees, it should route to uncertain."""
    flipped = answers(polarity_swapped={
        "type": "choice", "choice": "inverted", "confidence": 0.9,
        "probabilities": {"same": 0.05, "inverted": 0.94, "neither": 0.01}})
    r = route(flipped, Check("ok"), T)
    assert r.status == "uncertain" and any("swap test" in x for x in r.reasons)


def test_route_flags_divergence_kinds_and_implications():
    """When kinds pass list_min it should flag them; implications should be uncertain."""
    kinds = answers(divergence_kind={
        "type": "choice", "choice": "overtime", "confidence": 0.3,
        "probabilities": {"overtime": 0.41, "none": 0.5, "scope": 0.09}})
    assert route(kinds, Check("ok"), T).flags == ["overtime"]
    imp = route(answers(), Check("implication", "x", broader="pm"), T)
    assert (imp.status, imp.relation, imp.direction) == (
        "uncertain", "implication_candidate", "pm_broader")


def test_inputs_hash_changes_with_rules():
    """When a market's rules change, it should produce a different inputs hash."""
    assert inputs_hash(km("t", rules="a"), pm("t")) != inputs_hash(km("t", rules="b"), pm("t"))


@respx.mock
async def test_jev_client_sends_questions_and_retries():
    """When the API is overloaded once, it should retry and parse the answers."""
    route_ = respx.post(f"{API_URL}/v1/systemone")
    route_.side_effect = [
        httpx.Response(529, headers={"retry-after": "0"}),
        httpx.Response(200, json={"model": "jev-1.14.0", "answers": answers().raw}),
    ]
    client = JevClient("key", "jev-1.14.0")
    a = await client.classify(km("BTC"), pm("BTC"))
    await client.aclose()
    body = json.loads(route_.calls[1].request.content)
    assert set(body["questions"]) == set(build_questions())
    assert route_.calls[1].request.headers["authorization"] == "Bearer key"
    assert a.noul("same_event") == 0.99
