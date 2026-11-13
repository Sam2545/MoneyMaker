from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

import structlog

from moneymaker.models import Side, Venue
from moneymaker.orderbook import OrderBook
from moneymaker.venues.base import VenueClient
from moneymaker.venues.kalshi.client import KalshiClient
from moneymaker.venues.polymarket_us.client import PolymarketUSClient


def make_client(venue: Venue) -> VenueClient:
    """Returns the market data client for the given venue."""
    return KalshiClient() if venue is Venue.KALSHI else PolymarketUSClient()


def fmt_top(book: OrderBook) -> str:
    """Formats a book's best YES bid, best YES ask, and implied NO ask as one line."""
    bid, ask = book.best_bid(), book.best_ask()
    no_ask = book.walk(Side.NO, book.yes_bids[bid[0]]).avg_price if bid else None
    b = f"{bid[0]:.4f} x {bid[1]:>8}" if bid else "    -    x        -"
    a = f"{ask[0]:.4f} x {ask[1]:>8}" if ask else "    -    x        -"
    n = f"{no_ask:.4f}" if no_ask is not None else "  -   "
    return f"{book.venue_market_id:<48} YES bid {b} | YES ask {a} | NO ask {n}"


async def cmd_markets(venue: Venue, limit: int) -> None:
    """Prints open markets for a venue with their close date and title."""
    client = make_client(venue)
    try:
        for m in await client.list_markets(limit=limit):
            close = m.close_time.date().isoformat() if m.close_time else "?"
            print(f"{m.venue_market_id:<48} {close}  {m.title[:90]}")
    finally:
        await client.aclose()


async def cmd_book(venue: Venue, market_id: str, depth: int) -> None:
    """Prints a REST order book snapshot as side-by-side YES bid and ask ladders."""
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
    """Prints top-of-book for each market every time its live book updates."""
    client = make_client(venue)
    try:
        async for book in client.stream_orderbooks(market_ids):
            print(fmt_top(book), flush=True)
    finally:
        await client.aclose()


def print_match_summary(run) -> None:
    """Prints counts, the best-priced pairs, and the report path for a match run."""
    st = run.stats
    print(f"\nscope {run.scope}: {st.kalshi_markets} Kalshi x {st.pm_markets} PM US markets")
    print(f"candidates: {st.proposed} proposed → {st.kept} kept", end="")
    if st.rejected:
        print(" (dropped: " + ", ".join(f"{n} {why}" for why, n in st.rejected.most_common()) + ")")
    else:
        print()
    print(
        f"embeddings ({run.embedding_model}) computed: {run.embedded} · "
        f"Jev calls: {run.jev_calls} · "
        f"cached: {run.jev_cached}"
    )
    print("statuses: " + ", ".join(f"{k} {v}" for k, v in run.status_counts().most_common()))
    priced = [r for r in run.results if r.edge is not None and r.edge.top_marginal is not None]
    priced.sort(key=lambda r: (r.edge.net, r.edge.top_marginal), reverse=True)
    if priced:
        print(f"\n{'status':<20}{'net':>9}{'¢/ct':>8}{'size':>8}{'%/yr':>7}  {'top¢':>6}  pair")
        for r in priced[:15]:
            e = r.edge
            ann = f"{float(e.annualized) * 100:6.1f}" if e.annualized is not None else "     -"
            print(
                f"{r.status:<20}{float(e.net):>9.2f}{float(e.net_per_contract) * 100:>8.2f}"
                f"{float(e.contracts):>8.0f}{ann:>7}  {float(e.top_marginal) * 100:>6.2f}  "
                f"{r.candidate.kalshi.venue_market_id} ↔ {r.candidate.pm.venue_market_id}"
            )
    print(f"\nreport: {run.report_path}")
    print("review with: uv run moneymaker review")


def main(argv: Sequence[str] | None = None) -> None:
    """Parses CLI arguments, configures logging, and runs the selected command."""
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

    from moneymaker.matching.pipeline import SCOPES

    p = sub.add_parser("match", help="find, classify and price cross-venue market pairs")
    p.add_argument("--scope", choices=sorted(SCOPES), required=True)
    p.add_argument("--no-jev", action="store_true", help="skip Jev classification")
    p.add_argument("--jev-limit", type=int, help="maximum new Jev calls this run")
    p.add_argument("--window-days", type=float, default=30.0)
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--match", dest="include", help="only markets whose id or title matches REGEX")

    p = sub.add_parser(
        "match-templates", help="approve and price pairs of approved templates without AI calls"
    )
    p.add_argument("--scope", choices=sorted(SCOPES), required=True)
    p.add_argument("--match", dest="include", help="only markets whose id or title matches REGEX")
    p.add_argument("--full", action="store_true", help="fetch every Kalshi series in scope")

    p = sub.add_parser("monitor", help="stream approved pairs and record opportunity episodes")
    p.add_argument("--scope", choices=sorted(SCOPES), required=True)
    p.add_argument("--match", dest="include", help="only markets whose id or title matches REGEX")
    p.add_argument("--refresh-min", type=float, default=5.0,
                   help="minutes between template-matcher refreshes")
    p.add_argument("--duration-s", type=float, help="stop after this many seconds")
    p.add_argument("--log-dir", type=Path, default=Path("data/logs"),
                   help="directory for the daily opportunity summaries")

    p = sub.add_parser("opportunities", help="summarize recorded opportunity episodes")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--merge-gap", type=float, default=120.0,
                   help="merge same-pair, same-direction episodes split by at most this many s")
    p.add_argument("--min-seconds", type=float, default=2.0,
                   help="hide merged episodes shorter than this many seconds")
    p.add_argument("--all", dest="show_all", action="store_true",
                   help="show every merged episode regardless of duration")

    p = sub.add_parser("review", help="review matched pairs and record decisions")
    p.add_argument(
        "--status", action="append",
        help="statuses to review (repeatable); default pending_review, suspended, unclassified",
    )
    p.add_argument("--limit", type=int)
    p.add_argument("--sample", type=int, metavar="N", help="review the top N pairs per template")
    p.add_argument("--match", help="only pairs whose Kalshi or PM id matches REGEX")

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
        elif args.cmd == "stream":
            asyncio.run(cmd_stream(args.venue, args.market_ids))
        elif args.cmd == "match":
            from moneymaker.matching.pipeline import run_match

            run = asyncio.run(run_match(
                args.scope, use_jev=not args.no_jev, jev_limit=args.jev_limit,
                window_days=args.window_days, top_k=args.top_k, include=args.include,
            ))
            print_match_summary(run)
        elif args.cmd == "match-templates":
            from moneymaker.matching.templates import run_match_templates, summary_lines

            tpl_run = asyncio.run(run_match_templates(
                args.scope, include=args.include, force_full=args.full,
            ))
            print("\n".join(summary_lines(tpl_run)))
        elif args.cmd == "monitor":
            from moneymaker.monitor import run_monitor

            asyncio.run(run_monitor(
                args.scope, include=args.include, refresh_min=args.refresh_min,
                duration_s=args.duration_s, log_dir=args.log_dir,
            ))
        elif args.cmd == "opportunities":
            from moneymaker.config import get_settings
            from moneymaker.matching.store import MatchStore
            from moneymaker.monitor import opportunities_report

            store = MatchStore(get_settings().match_db_path)
            try:
                print(opportunities_report(
                    store, args.hours, merge_gap_s=args.merge_gap, min_seconds=args.min_seconds,
                    show_all=args.show_all,
                ), end="")
            finally:
                store.close()
        else:
            from moneymaker.config import get_settings
            from moneymaker.matching.review import DEFAULT_STATUSES, review
            from moneymaker.matching.store import MatchStore

            store = MatchStore(get_settings().match_db_path)
            try:
                counts = review(
                    store, args.status or DEFAULT_STATUSES, limit=args.limit, sample=args.sample,
                    match=args.match,
                )
            finally:
                store.close()
            print("\n" + ", ".join(f"{k} {v}" for k, v in counts.items() if v))
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        raise SystemExit(f"error: {exc}") from None


if __name__ == "__main__":
    main()
