import base64
from decimal import Decimal as D

import httpx
import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from moneymaker.config import Settings
from moneymaker.models import MarketStatus, Side
from moneymaker.orderbook import OrderBook
from moneymaker.venues.polymarket_us.auth import PolymarketUSSigner
from moneymaker.venues.polymarket_us.client import (
    GATEWAY_URL,
    PolymarketUSClient,
    apply_market_data,
    parse_market,
)

MARKET = {
    "id": "7974",
    "slug": "tec-mls-winner-2026-11-07-vwh",
    "question": "Major League Soccer Champion",
    "title": "Vancouver Whitecaps FC",
    "description": "Will Vancouver Whitecaps FC win the 2026 MLS Cup?",
    "endDate": "2027-01-01T21:20:09Z",
    "status": "MARKET_STATUS_OPEN",
    "orderPriceMinTickSize": 0.001,
    "feeCoefficient": 0.0695,
}

BOOK = {
    "marketData": {
        "marketSlug": "tec-mls-winner-2026-11-07-vwh",
        "bids": [
            {"px": {"value": "0.1810", "currency": "USD"}, "qty": "1.0000"},
            {"px": {"value": "0.1800", "currency": "USD"}, "qty": "12.0000"},
        ],
        "offers": [{"px": {"value": "0.1820", "currency": "USD"}, "qty": "2.0000"}],
        "transactTime": "2026-11-03T01:49:31.710827791Z",
    }
}


def test_parse_market():
    m = parse_market(MARKET)
    assert m.venue_market_id == MARKET["slug"]
    assert m.title == "Major League Soccer Champion: Vancouver Whitecaps FC"
    assert m.status is MarketStatus.OPEN
    assert m.tick_size == D("0.001")
    assert m.fee_coefficient == D("0.0695")
    assert m.close_time.year == 2027


def test_apply_market_data_yes_perspective():
    book = OrderBook(parse_market(MARKET).venue, MARKET["slug"])
    apply_market_data(book, BOOK["marketData"])
    assert book.best_bid() == (D("0.1810"), D("1.0000"))
    assert book.best_ask() == (D("0.1820"), D("2.0000"))
    assert book.walk(Side.NO, D(1)).avg_price == D("0.8190")
    assert book.exchange_ts.microsecond == 710827


def test_signer_produces_verifiable_signature():
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    signer = PolymarketUSSigner("kid", base64.b64encode(seed).decode())
    h = signer.headers("get", "/v1/ws/markets", timestamp_ms=1730000000000)
    assert h["X-PM-Access-Key"] == "kid"
    assert h["X-PM-Timestamp"] == "1730000000000"
    key.public_key().verify(
        base64.b64decode(h["X-PM-Signature"]), b"1730000000000GET/v1/ws/markets"
    )


@respx.mock
async def test_list_markets_paginates_and_get_orderbook():
    page1 = [{**MARKET, "slug": f"m{i}"} for i in range(100)]
    page2 = [{**MARKET, "slug": "last"}]
    respx.get(f"{GATEWAY_URL}/v1/markets", params={"offset": "0"}).mock(
        return_value=httpx.Response(200, json={"markets": page1})
    )
    respx.get(f"{GATEWAY_URL}/v1/markets", params={"offset": "100"}).mock(
        return_value=httpx.Response(200, json={"markets": page2})
    )
    respx.get(f"{GATEWAY_URL}/v1/markets/last/book").mock(
        return_value=httpx.Response(200, json=BOOK)
    )
    client = PolymarketUSClient(Settings(_env_file=None))
    markets = await client.list_markets()
    assert len(markets) == 101
    book = await client.get_orderbook("last")
    assert book.best_ask() == (D("0.1820"), D("2.0000"))
    await client.aclose()
