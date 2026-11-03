import base64
from decimal import Decimal as D

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from moneymaker.config import Settings
from moneymaker.models import MarketStatus, Side, Venue
from moneymaker.orderbook import OrderBook
from moneymaker.venues.kalshi.auth import KalshiSigner
from moneymaker.venues.kalshi.client import KalshiClient, apply_ws_delta, load_bids, parse_market

MARKET = {
    "ticker": "KXFED-26DEC-T3.75",
    "event_ticker": "KXFED-26DEC",
    "market_type": "binary",
    "title": "Fed funds rate above 3.75%?",
    "yes_sub_title": "Above 3.75%",
    "status": "active",
    "close_time": "2026-12-10T19:00:00Z",
    "latest_expiration_time": "2026-12-17T19:00:00Z",
    "rules_primary": "If the upper bound is above 3.75% ... resolves to Yes.",
    "rules_secondary": "Source: FOMC statement.",
    "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
}


def test_parse_market():
    m = parse_market(MARKET)
    assert m.venue is Venue.KALSHI
    assert m.event_id == "KXFED-26DEC"
    assert m.title == "Fed funds rate above 3.75%?: Above 3.75%"
    assert m.status is MarketStatus.OPEN
    assert m.tick_size == D("0.01")
    assert "FOMC" in m.rules
    assert m.expiration_time.day == 17


def test_load_bids_inverts_no_side():
    book = OrderBook(Venue.KALSHI, "T")
    load_bids(
        book,
        yes=[["0.0100", "200.00"], ["0.4200", "13.00"]],
        no=[["0.0100", "100.00"], ["0.5600", "17.00"]],
    )
    assert book.best_bid() == (D("0.4200"), D("13.00"))
    assert book.best_ask() == (D("0.4400"), D("17.00"))
    assert book.walk(Side.NO, D(13)).avg_price == D("0.5800")
    assert book.synced


def test_ws_delta_routes_sides():
    book = OrderBook(Venue.KALSHI, "T")
    load_bids(book, yes=[["0.4200", "13.00"]], no=[["0.5600", "17.00"]])
    apply_ws_delta(book, {"price_dollars": "0.4200", "delta_fp": "-13.00", "side": "yes"})
    assert book.best_bid() is None
    apply_ws_delta(book, {"price_dollars": "0.5700", "delta_fp": "5.00", "side": "no",
                          "ts_ms": 1669149841000})
    assert book.best_ask() == (D("0.4300"), D("5.00"))


@pytest.mark.parametrize("kind", ["rsa", "ed25519"])
def test_signer_signs_path_without_query(kind):
    key = rsa.generate_private_key(65537, 2048) if kind == "rsa" else Ed25519PrivateKey.generate()
    h = KalshiSigner("kid", key).headers("get", "/trade-api/v2/markets?limit=5", 1703123456789)
    sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
    msg = b"1703123456789GET/trade-api/v2/markets"
    if kind == "rsa":
        key.public_key().verify(
            sig, msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    else:
        key.public_key().verify(sig, msg)
    assert h["KALSHI-ACCESS-KEY"] == "kid"


@respx.mock
async def test_list_markets_follows_cursor_and_filters_non_binary():
    settings = Settings(_env_file=None)
    base = settings.kalshi_rest_url
    route = respx.get(f"{base}/markets")
    route.side_effect = [
        httpx.Response(200, json={"markets": [MARKET, {**MARKET, "ticker": "X",
                                                       "market_type": "scalar"}], "cursor": "c1"}),
        httpx.Response(200, json={"markets": [{**MARKET, "ticker": "Y"}], "cursor": ""}),
    ]
    respx.get(f"{base}/markets/Y/orderbook").mock(return_value=httpx.Response(
        200, json={"orderbook_fp": {"yes_dollars": [["0.30", "5.00"]], "no_dollars": []}}))
    client = KalshiClient(settings)
    markets = await client.list_markets()
    assert [m.venue_market_id for m in markets] == ["KXFED-26DEC-T3.75", "Y"]
    assert route.calls[1].request.url.params["cursor"] == "c1"
    book = await client.get_orderbook("Y")
    assert book.best_bid() == (D("0.30"), D("5.00"))
    assert book.best_ask() is None
    await client.aclose()
