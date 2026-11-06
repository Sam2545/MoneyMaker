import json

import httpx
import numpy as np
import pytest
import respx

from moneymaker.matching.candidates import generate
from moneymaker.matching.embed import Embedder, market_text
from moneymaker.matching.extract import Check, compare, describe
from moneymaker.matching.gemini import API_URL as GEMINI_URL
from moneymaker.matching.gemini import GeminiEncoder
from moneymaker.matching.openrouter import API_URL as OR_URL
from moneymaker.matching.openrouter import OpenRouterEncoder
from moneymaker.matching.review import review
from moneymaker.matching.store import MatchStore, Routed
from moneymaker.models import Market, Venue


def km(title: str, yes: str | None = None, ticker: str = "K1", rules: str = "") -> Market:
    """Returns a Kalshi market with the given title and YES label."""
    return Market(venue=Venue.KALSHI, venue_market_id=ticker, title=title, yes_label=yes,
                  rules=rules)


def pm(title: str, yes: str | None = None, slug: str = "P1", rules: str = "") -> Market:
    """Returns a PM US market with the given title and YES label."""
    return Market(venue=Venue.POLYMARKET_US, venue_market_id=slug, title=title, yes_label=yes,
                  rules=rules)


@pytest.mark.parametrize(
    ("kalshi", "poly", "result"),
    [
        (km("BTC EOY: 20,000 to 24,999.99", "20,000 to 24,999.99"),
         pm("Bitcoin end of 2026?: 20,000 to 24,999.99", "20,000 to 24,999.99"), "ok"),
        (km("BTC EOY: 20,000 to 24,999.99", "20,000 to 24,999.99"),
         pm("Bitcoin end of 2026?: 25,000 to 29,999.99", "25,000 to 29,999.99"), "reject"),
        (km("BTC: 150,000 or above", "150,000 or above"),
         pm("How high?: Above $149,999.99", "Above $149,999.99"), "ok"),
        (km("BTC EOY: 20,000 to 24,999.99", "20,000 to 24,999.99"),
         pm("How high?: Above $139,999.99", "Above $139,999.99"), "reject"),
        (km("LaMelo Ball: 45+ points", "45+ points"), pm("LaMelo Ball: 40+ points", "40+"),
         "implication"),
        (km("Ben Shelton wins", "Ben Shelton"),
         pm("Who will win?: Taylor Fritz vs Carlos Alcaraz", "Taylor Fritz vs Carlos Alcaraz"),
         "reject"),
        (km("Taylor Fritz wins", "Taylor Fritz"),
         pm("Who will win?: Taylor Fritz vs Carlos Alcaraz", "Taylor Fritz vs Carlos Alcaraz"),
         "ok"),
        (km("BTC up in next 15 mins at 7:30 PM Nov 5, 2026?"), pm("BTC Up or Down 15m 2026"),
         "ok"),
    ],
)
def test_shape_compare(kalshi, poly, result):
    """When comparing parsed shapes, it should accept, reject, or flag implications correctly."""
    assert compare(describe(kalshi), describe(poly)).result == result


def test_threshold_implication_names_broader_side():
    """When thresholds share a direction, it should mark the lower up-strike as broader."""
    check = compare(describe(km("BTC above $150k", "Above $150k")),
                    describe(pm("BTC above $140k", "Above $140k")))
    assert check == Check("implication", check.reason, broader="pm")


def fake_encoder(vocab: list[str]):
    """Returns an encoder mapping each text to a bag-of-words vector over vocab."""
    def encode(texts):
        """Encodes texts as word-count vectors."""
        return np.array(
            [[t.lower().count(w) for w in vocab] for t in texts], dtype=np.float32
        ) + 1e-3
    return encode


def test_embedder_caches_vectors():
    """When embedding the same text twice, it should compute it only once."""
    store = MatchStore(":memory:")
    calls = []
    def encode(texts):
        """Records calls and returns constant vectors."""
        calls.append(list(texts))
        return np.ones((len(texts), 3), dtype=np.float32)
    e = Embedder(store, "fake", encode=encode)
    e.embed(["a", "b"])
    e.embed(["a", "b", "c"])
    assert calls == [["a", "b"], ["c"]]
    assert e.computed == 3


def test_candidates_skip_more_similar_conflicts():
    """When the most similar market is a different range, it should still find the matching one."""
    store = MatchStore(":memory:")
    e = Embedder(store, "fake", encode=fake_encoder(["bitcoin", "range", "eoy", "fritz"]))
    kalshi = [
        km("Bitcoin range EOY: 20,000 to 24,999.99", "20,000 to 24,999.99", "K-A"),
        km("Bitcoin range bitcoin range EOY: 25,000 to 29,999.99", "25,000 to 29,999.99", "K-B"),
        km("Tennis: Fritz wins", "Taylor Fritz", "K-T"),
    ]
    poly = [pm("Bitcoin range bitcoin range end of 2026?: 20,000 to 24,999.99",
               "20,000 to 24,999.99", "P-A")]
    kv = e.embed([market_text(m) for m in kalshi])
    pv = e.embed([market_text(m) for m in poly])
    assert (pv @ kv.T)[0, 1] > (pv @ kv.T)[0, 0]
    cands, stats = generate(kalshi, poly, e, top_k=1)
    assert [(c.kalshi.venue_market_id, c.pm.venue_market_id) for c in cands] == [("K-A", "P-A")]
    assert stats.rejected["different ranges"] == 1


def test_store_keeps_user_decision_and_suspends_on_change():
    """When inputs are unchanged it should keep the user decision; when they change, suspend."""
    s = MatchStore(":memory:")
    s.upsert_routed("K", "P", "h1", 0.9, Routed("pending_review", "equivalent", "same"))
    s.record_decision("K", "P", "approved_equivalent", relation="equivalent", polarity="same")
    assert s.upsert_routed("K", "P", "h1", 0.9, Routed("uncertain")) == "approved_equivalent"
    assert s.upsert_routed("K", "P", "h2", 0.9, Routed("pending_review")) == "suspended"


@respx.mock
def test_gemini_encoder_batches_with_similarity_task():
    """When embedding with Gemini, it should batch requests and set the similarity task."""
    route_ = respx.post(f"{GEMINI_URL}/models/gemini-embedding-2:batchEmbedContents").mock(
        side_effect=lambda request: httpx.Response(200, json={"embeddings": [
            {"values": [float(i), 1.0]} for i in range(len(json.loads(request.content)["requests"]))
        ]}))
    enc = GeminiEncoder("key", batch_size=2)
    out = enc(["a", "b", "c"])
    enc.close()
    first = json.loads(route_.calls[0].request.content)["requests"][0]
    assert route_.call_count == 2 and out.shape == (3, 2)
    assert first["content"]["parts"][0]["text"] == "task: sentence similarity | query: a"
    assert first["output_dimensionality"] == 768
    assert route_.calls[0].request.headers["x-goog-api-key"] == "key"
    legacy = GeminiEncoder("key", "gemini-embedding-001")._request("a")
    assert legacy["task_type"] == "SEMANTIC_SIMILARITY"
    assert legacy["content"]["parts"][0]["text"] == "a"


@respx.mock
def test_openrouter_encoder_batches_and_orders():
    """When embedding more texts than a batch, it should split requests and keep input order."""
    def reply(request):
        """Returns embeddings in reversed order with indexes."""
        n = len(json.loads(request.content)["input"])
        data = [{"object": "embedding", "index": i, "embedding": [float(i), 1.0]} for i in range(n)]
        return httpx.Response(200, json={"object": "list", "data": data[::-1], "model": "m"})
    route_ = respx.post(f"{OR_URL}/embeddings").mock(side_effect=reply)
    enc = OpenRouterEncoder("key", "nvidia/nemotron-3.5-embed-8b", batch_size=2)
    out = enc(["a", "b", "c"])
    enc.close()
    assert route_.call_count == 2
    assert out[:, 0].tolist() == [0.0, 1.0, 0.0]


def test_review_records_decisions():
    """When the reviewer approves one pair and rejects another, it should store both decisions."""
    s = MatchStore(":memory:")
    s.put_markets([km("A", ticker="K1"), pm("A", slug="P1"), km("B", ticker="K2"),
                   pm("B", slug="P2")])
    s.upsert_routed("K1", "P1", "h", 0.9, Routed("pending_review", "equivalent", "same"))
    s.upsert_routed("K2", "P2", "h", 0.8, Routed("pending_review", "equivalent", "same"))
    replies = iter(["e", "", "", "r", "different events"])
    counts = review(s, input_fn=lambda _: next(replies), print_fn=lambda _: None)
    assert counts["equivalent"] == 1 and counts["rejected"] == 1
    assert s.get_match("K1", "P1")["status"] == "approved_equivalent"
    row = s.get_match("K2", "P2")
    assert (row["status"], row["note"], row["decided_by"]) == (
        "rejected", "different events", "user"
    )
