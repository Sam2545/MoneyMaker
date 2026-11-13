import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from test_autoapprove import ETH_K, ETH_P, market

from moneymaker.edge import Edge
from moneymaker.matching.store import MatchStore, Routed
from moneymaker.models import Venue
from moneymaker.monitor import Monitor, MonitoredPair
from moneymaker.opportunities import (
    EpisodeTracker,
    bucket,
    merge_episodes,
    render_markdown,
    render_text,
    summarize,
    write_daily,
)
from moneymaker.orderbook import OrderBook

T0 = datetime(2026, 11, 7, 12, tzinfo=UTC)


class Clock:
    """A manually advanced clock."""

    def __init__(self) -> None:
        """Starts at T0."""
        self.now = T0

    def __call__(self) -> datetime:
        """Returns the current fake time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Moves the clock forward."""
        self.now += timedelta(seconds=seconds)


def kalshi_book(yes_ask: str, size: int = 100) -> OrderBook:
    """Returns a synced Kalshi book with one YES ask and a YES bid a cent below."""
    b = OrderBook(Venue.KALSHI, ETH_K)
    b.replace([(D(yes_ask) - D("0.01"), D(size))], [(D(yes_ask), D(size))])
    return b


def pm_book(yes_bid: str, size: int = 100) -> OrderBook:
    """Returns a synced PM book with one YES bid and a YES ask a cent above."""
    b = OrderBook(Venue.POLYMARKET_US, ETH_P)
    b.replace([(D(yes_bid), D(size))], [(D(yes_bid) + D("0.01"), D(size))])
    return b


def monitored(status: str = "auto_approved", routed: Routed | None = None) -> MonitoredPair:
    """Returns the ETH fixture pair as a monitored equivalent with polarity same."""
    routed = routed or Routed(status, "equivalent", "same")
    return MonitoredPair(market(ETH_K), market(ETH_P), status, routed, "eth", "KXETHD <-> x")


def setup() -> tuple[Monitor, Clock, MatchStore]:
    """Returns a monitor on an in-memory store watching the ETH pair, with its clock."""
    store = MatchStore(":memory:")
    clock = Clock()
    mon = Monitor(store, clock=clock)
    assert mon.set_pairs([monitored()]) == ({ETH_K}, {ETH_P})
    return mon, clock, store


def stored(store: MatchStore) -> list[dict]:
    """Returns every stored opportunity row."""
    return store.opportunities_between("0", "9")


def test_episode_opens_on_profit_and_closes_when_it_stops():
    """When a scripted book sequence turns profitable and back, it should store one episode."""
    mon, clock, store = setup()
    mon.on_book("kalshi", kalshi_book("0.40"))
    assert not mon.tracker.open
    clock.advance(0.5)
    mon.on_book("pm", pm_book("0.50"))
    assert set(mon.tracker.open) == {(ETH_K, ETH_P)}
    clock.advance(2)
    mon.on_book("kalshi", kalshi_book("0.38", size=400))
    clock.advance(1)
    mon.on_book("pm", pm_book("0.40"))
    assert not mon.tracker.open
    [row] = stored(store)
    assert row["duration_s"] == 3.0
    assert row["label"] == "K-YES + PM-NO"
    assert row["max_contracts"] == 50
    assert row["updates"] == 2
    assert row["max_net"] > 0 and row["capital_at_max"] > 0
    assert (row["asset"], row["template"]) == ("eth", "KXETHD <-> x")


def test_unprofitable_or_tiny_edges_do_not_open_episodes():
    """When the edge is negative or sizes under one contract, it should not open an episode."""
    mon, _, store = setup()
    mon.on_book("kalshi", kalshi_book("0.49"))
    mon.on_book("pm", pm_book("0.50"))
    assert not mon.tracker.open
    mon.on_book("kalshi", kalshi_book("0.40", size=1))
    assert not mon.tracker.open
    assert stored(store) == []


def test_lost_sync_or_dropped_pair_closes_the_episode():
    """When a book goes unsynced or the pair is dropped, it should close the open episode."""
    mon, clock, store = setup()
    k = kalshi_book("0.40")
    mon.on_book("kalshi", k)
    mon.on_book("pm", pm_book("0.50"))
    clock.advance(4)
    k.invalidate()
    mon.sweep()
    assert not mon.tracker.open and len(stored(store)) == 1
    mon.on_book("kalshi", kalshi_book("0.40"))
    assert mon.tracker.open
    clock.advance(70)
    assert mon.set_pairs([]) == (set(), set())
    assert not mon.tracker.open and not mon.books
    assert [r["duration_s"] for r in stored(store)] == [4.0, 70.0]


def test_implication_pairs_price_only_the_safe_direction():
    """When the pair is a user implication, it should only open episodes in the safe direction."""
    store = MatchStore(":memory:")
    mon = Monitor(store, clock=Clock())
    routed = Routed("approved_implication", "implication", "same", "kalshi_broader")
    mon.set_pairs([monitored("approved_implication", routed)])
    mon.on_book("kalshi", kalshi_book("0.80"))
    mon.on_book("pm", pm_book("0.10"))
    assert not mon.tracker.open
    mon.on_book("kalshi", kalshi_book("0.40"))
    mon.on_book("pm", pm_book("0.50"))
    assert mon.tracker.open[(ETH_K, ETH_P)].label == "K-YES + PM-NO (implication)"


def test_bad_pair_does_not_crash(monkeypatch):
    """When pricing a pair raises, it should log, count the error and keep going."""
    mon, _, _ = setup()

    def boom(*args, **kwargs):
        """Raises like a corrupt book would."""
        raise ValueError("bad book")

    monkeypatch.setattr("moneymaker.monitor.price_edge", boom)
    mon.on_book("kalshi", kalshi_book("0.40"))
    mon.on_book("pm", pm_book("0.50"))
    assert mon.errors == 1 and not mon.tracker.open


def edge(net: str, contracts: str = "10") -> Edge:
    """Returns an edge with the given net and contracts."""
    return Edge("L", D(contracts), D("9"), D("0.1"), D(net), D(30), D("0.01"), (), ())


def test_tracker_keeps_maxima_and_label_at_max():
    """When live edges vary, it should keep each maximum and the label of the best net."""
    closed = []
    tracker = EpisodeTracker(closed.append)
    tracker.update(("k", "p"), edge("0.5", "20"), T0)
    tracker.update(("k", "p"), edge("0.9", "10"), T0 + timedelta(seconds=1))
    tracker.update(("k", "p"), None, T0 + timedelta(seconds=2))
    [ep] = closed
    assert (ep.max_net, ep.max_contracts, ep.updates) == (0.9, 20.0, 2)
    assert ep.row()["duration_s"] == 2.0


def row(
    asset: str, template: str, duration: float, net: float, i: int = 0, *,
    start: float | None = None, kalshi: str | None = None, pm: str | None = None,
    label: str = "K-YES + PM-NO",
) -> dict:
    """Returns an opportunity row for summary tests, starting start seconds after T0."""
    t = T0 + timedelta(seconds=i if start is None else start)
    return {
        "kalshi_id": kalshi or f"K{i}", "pm_id": pm or f"P{i}", "label": label, "asset": asset,
        "template": template, "started_at": t.isoformat(timespec="milliseconds"),
        "ended_at": (t + timedelta(seconds=duration)).isoformat(timespec="milliseconds"),
        "duration_s": duration, "max_cents_per_contract": 1.0,
        "max_contracts": 10.0, "max_net": net, "capital_at_max": 9.0,
        "annualized_at_max": None, "updates": 3,
    }


def test_summary_aggregates_counts_buckets_dollars_and_top(tmp_path):
    """When episodes span assets, templates and durations, it should aggregate each view."""
    rows = [
        row("btc", "A", 0.2, 1.0, 1), row("btc", "A", 3.0, 3.0, 2),
        row("eth", "B", 30.0, 0.5, 3), row("eth", "A", 120.0, 10.0, 4),
    ]
    s = summarize(rows, start=T0.isoformat(), end=T0.isoformat())
    assert s["episodes"] == 4
    assert s["total_net"] == 14.5 and s["median_net"] == 2.0
    assert s["by_asset"] == {"btc": {"count": 2, "total_net": 4.0},
                             "eth": {"count": 2, "total_net": 10.5}}
    assert s["by_template"]["A"] == {"count": 3, "total_net": 14.0}
    assert s["durations"] == {"< 1 s": 1, "1-5 s": 1, "5-60 s": 1, "> 60 s": 1}
    assert [r["pm_id"] for r in s["top"]] == ["P4", "P2", "P1", "P3"]
    assert bucket(5.0) == "5-60 s" and bucket(60.0) == "> 60 s"
    md = render_markdown(s)
    assert "K4" in md and "↳ P4" in md
    json_path, md_path = write_daily(s, tmp_path, "2026-11-07")
    assert json.loads(json_path.read_text())["episodes"] == 4
    assert md_path.read_text() == md


def test_empty_summary_is_zero():
    """When there are no episodes, it should report zero totals."""
    s = summarize([], start="s", end="e")
    assert (s["episodes"], s["total_net"], s["median_net"]) == (0, 0, 0.0)


def test_restart_splits_merge_into_one_episode():
    """When one pair's episodes are split by short gaps, it should merge them into one."""
    pair = {"kalshi": "K", "pm": "P"}
    rows = [
        row("btc", "A", 10.0, 1.0, start=0, **pair),
        row("btc", "A", 5.0, 4.0, start=60, **pair),
        row("btc", "A", 1.0, 2.0, start=100, **pair),
        row("btc", "A", 1.0, 0.5, start=400, **pair),
    ]
    first, second = merge_episodes(rows, 120.0)
    assert (first["merged"], first["duration_s"], first["max_net"]) == (3, 101.0, 4.0)
    assert first["updates"] == 9 and first["ended_at"] == rows[2]["ended_at"]
    assert (second["merged"], second["duration_s"]) == (1, 1.0)


def test_distinct_directions_and_pairs_do_not_merge():
    """When adjacent episodes differ in direction or pair, it should keep them apart."""
    rows = [
        row("btc", "A", 1.0, 1.0, start=0, kalshi="K", pm="P"),
        row("btc", "A", 1.0, 1.0, start=2, kalshi="K", pm="P", label="K-NO + PM-YES"),
        row("btc", "A", 1.0, 1.0, start=4, kalshi="K", pm="Q"),
    ]
    assert [r["merged"] for r in merge_episodes(rows, 120.0)] == [1, 1, 1]


def test_filter_counts_apply_after_merging():
    """When short raw episodes merge past the minimum, it should count them as kept."""
    rows = [
        row("btc", "A", 1.0, 1.0, start=0, kalshi="K", pm="P"),
        row("btc", "A", 1.0, 2.0, start=3, kalshi="K", pm="P"),
        row("eth", "B", 0.5, 5.0, start=0, kalshi="E", pm="F"),
    ]
    s = summarize(rows, start=T0.isoformat(), end=T0.isoformat(), merge_gap_s=120.0,
                  min_seconds=2.0)
    assert (s["episodes"], s["merged_episodes"], s["filtered_episodes"]) == (3, 2, 1)
    assert s["filtered_total_net"] == 2.0 and s["filtered_median_net"] == 2.0
    assert s["filtered_by_asset"] == {
        "btc": {"episodes": 1, "filtered": 1, "filtered_net": 2.0},
        "eth": {"episodes": 1, "filtered": 0, "filtered_net": 0.0},
    }
    assert s["merged_durations"]["< 1 s"] == 1 and s["merged_durations"]["1-5 s"] == 1
    assert [r["kalshi_id"] for r in s["filtered_top"]] == ["K"]
    assert summarize(rows, start="s", end="e", min_seconds=0.0)["filtered_episodes"] == 2
    text = render_text(s)
    assert "3 raw episodes → 2 merged (gaps ≤ 120 s) → 1 lasting ≥ 2 s" in text


def test_rendered_tables_have_aligned_columns():
    """When a summary renders, it should give every line of a table the same width."""
    rows = [
        row("btc", "Template A", 30.0, 1234.5, 1), row("ethereum", "B", 3.0, 0.25, 2),
        row("sol", "C", 4000.0, 10.0, 3, kalshi="KXSOLD-26NOV1317-T99.9999-EXTRA-LONG-ID"),
    ]
    s = summarize(rows, start=T0.isoformat(), end=T0.isoformat())
    text = render_text(s)
    blocks = [b.splitlines() for b in text.split("\n\n")]
    tables = [b[1:] for b in blocks if len(b) > 2 and set(b[2]) <= {"─", " "}]
    assert len(tables) == 5
    for table in tables:
        table = [line for line in table if not line.startswith(("¢/ct", "max "))]
        assert len({len(line) for line in table}) == 1, table
    assert "1,234.50" in text and "01:06:40" not in text and "11-07 12:00:01" in text
    assert max(len(line) for line in text.splitlines()) <= 120


async def test_streams_resubscribe_only_when_ids_change(monkeypatch):
    """When the id set changes, it should restart the stream, and restart it after failures."""
    import asyncio

    from moneymaker import monitor as mon_mod
    from moneymaker.monitor import Streams

    monkeypatch.setattr(mon_mod, "STREAM_RETRY_S", 0.01)
    calls: list[list[str]] = []
    seen: list[str] = []

    async def factory(ids):
        """Yields one book, then fails like a broken connection."""
        calls.append(list(ids))
        yield kalshi_book("0.40")
        raise RuntimeError("socket died")

    streams = Streams({"kalshi": factory}, lambda venue, book: seen.append(venue))
    assert await streams.set("kalshi", {"a"})
    assert not await streams.set("kalshi", {"a"})
    await asyncio.sleep(0.05)
    assert len(calls) >= 2 and seen
    assert await streams.set("kalshi", {"a", "b"})
    await asyncio.sleep(0.02)
    assert calls[-1] == ["a", "b"]
    await streams.stop_all()
    assert not streams.tasks
