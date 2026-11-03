from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence

import structlog

from moneymaker.models import Side, Venue
from moneymaker.orderbook import OrderBook
from moneymaker.venues.base import VenueClient
from moneymaker.venues.kalshi.client import KalshiClient
from moneymaker.venues.polymarket_us.client import PolymarketUSClient


def make_client(venue: Venue) -> VenueClient:
    return KalshiClient() if venue is Venue.KALSHI else PolymarketUSClient()


def fmt_top(book: OrderBook) -> str:
    bid, ask = book.best_bid(), book.best_ask()
    no_ask = book.walk(Side.NO, book.yes_bids[bid[0]]).avg_price if bid else None
    b = f"{bid[0]:.4f} x {bid[1]:>8}" if bid else "    -    x        -"
    a = f"{ask[0]:.4f} x {ask[1]:>8}" if ask else "    -    x        -"
    n = f"{no_ask:.4f}" if no_ask is not None else "  -   "
    return f"{book.venue_market_id:<48} YES bid {b} | YES ask {a} | NO ask {n}"


async def cmd_markets(venue: Venue, limit: int) -> None:
    client = make_client(venue)
    try:
        for m in await client.list_markets(limit=limit):
            close = m.close_time.date().isoformat() if m.close_time else "?"
            print(f"{m.venue_market_id:<48} {close}  {m.title[:90]}")
    finally:
        await client.aclose()


async def cmd_book(venue: Venue, market_id: str, depth: int) -> None:
    client = make_client(venue)
    try:
        snap = (await client.get_orderbook(market_id)).snapshot(depth)
        print(f"{'YES bids':>24}   {'YES asks':<24}")
        for i in range(max(len(snap.yes_bids), len(snap.yes_asks))):
            b = snap.yes_bids[i] if i < len(snap.yes_bids) else None
            a = snap.yes_asks[i] if i < len(snap.yes_asks) else None
            left = f"{b.size:>10} @ {b.price:.4f}" if b else ""
            right = f"{a.price:.4f} @ {a.size:<10}" if a else ""
            print(f"{left:>24}   {right:<24}")
    finally:
        await client.aclose()


async def cmd_stream(venue: Venue, market_ids: Sequence[str]) -> None:
    client = make_client(venue)
    try:
        async for book in client.stream_orderbooks(market_ids):
            print(fmt_top(book), flush=True)
    finally:
        await client.aclose()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="moneymaker")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("markets", help="list open markets")
    p.add_argument("venue", type=Venue)
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("book", help="print a REST order book snapshot")
    p.add_argument("venue", type=Venue)
    p.add_argument("market_id")
    p.add_argument("--depth", type=int, default=10)

    p = sub.add_parser("stream", help="stream live top-of-book")
    p.add_argument("venue", type=Venue)
    p.add_argument("market_ids", nargs="+")

    args = parser.parse_args(argv)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if args.verbose else logging.INFO
        )
    )
    try:
        if args.cmd == "markets":
            asyncio.run(cmd_markets(args.venue, args.limit))
        elif args.cmd == "book":
            asyncio.run(cmd_book(args.venue, args.market_id, args.depth))
        else:
            asyncio.run(cmd_stream(args.venue, args.market_ids))
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        raise SystemExit(f"error: {exc}") from None


if __name__ == "__main__":
    main()
