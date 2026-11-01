"""The Polymarket market and sports feeds against local fake WebSocket servers that speak the documented protocol.

They prove our side (framing, PING/PONG, local book, reconnect). They cannot prove the real servers behave the
same way: run ``scripts/live_hub_check.py`` once on a real machine (see WORKLOG.md).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

from mm_agent.live.common import FeedStats, Quote, backoff_delay
from mm_agent.live.history import fetch_history
from mm_agent.live.polymarket_market_ws import PolymarketMarketFeed
from mm_agent.live.polymarket_sports_ws import PolymarketSportsFeed, parse_game, period_label

Handler = Callable[[ServerConnection], Awaitable[None]]


@asynccontextmanager
async def fake_server(handler: Handler) -> AsyncIterator[str]:
    async with serve(handler, "127.0.0.1", 0) as server:
        yield f"ws://127.0.0.1:{next(iter(server.sockets)).getsockname()[1]}"


async def until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    async def wait() -> None:
        while not pred():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


def book(asset: str, bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> str:
    return json.dumps(
        {
            "event_type": "book",
            "asset_id": asset,
            "bids": [{"price": p, "size": s} for p, s in bids],
            "asks": [{"price": p, "size": s} for p, s in asks],
            "timestamp": "1700000000000",
        }
    )


def change(asset: str, side: str, price: str, size: str) -> str:
    return json.dumps(
        {
            "event_type": "price_change",
            "price_changes": [{"asset_id": asset, "side": side, "price": price, "size": size}],
        }
    )


def market_feed(url: str, quotes: list[Quote], **kw: Any) -> PolymarketMarketFeed:
    return PolymarketMarketFeed(url, on_quote=quotes.append, backoff_base=0.01, backoff_cap=0.02, **kw)


# ------------------------------------------------------------------------------------------ market feed
async def test_subscribe_frame_then_book_and_price_changes_with_size_zero_removal() -> None:
    seen: list[Any] = []
    quotes: list[Quote] = []

    async def handler(ws: ServerConnection) -> None:
        seen.append(json.loads(await ws.recv()))
        await ws.send(book("t1", [("0.39", "100"), ("0.38", "50")], [("0.41", "80")]))
        await ws.send(change("t1", "BUY", "0.39", "0"))  # size "0" removes the best bid
        await ws.send(change("t1", "SELL", "0.40", "25"))  # a new best ask
        await ws.wait_closed()

    async with fake_server(handler) as url:
        feed = market_feed(url, quotes)
        feed.watch(["t1"])
        feed.start()
        await until(lambda: len(quotes) >= 3)
        await feed.stop()
    assert seen[0] == {"assets_ids": ["t1"], "type": "market", "custom_feature_enabled": True}
    assert (quotes[0].bid, quotes[0].ask) == (Decimal("0.39"), Decimal("0.41"))
    assert (quotes[1].bid, quotes[1].ask) == (Decimal("0.38"), Decimal("0.41"))
    assert (quotes[2].bid, quotes[2].ask) == (Decimal("0.38"), Decimal("0.40"))


async def test_ping_is_sent_and_pong_is_accepted() -> None:
    frames: list[str] = []
    quotes: list[Quote] = []

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        async for raw in ws:
            frames.append(str(raw))
            if raw == "PING":
                await ws.send("PONG")
                await ws.send(book("t1", [("0.10", "1")], [("0.12", "1")]))

    async with fake_server(handler) as url:
        feed = market_feed(url, quotes, ping_seconds=0.05)
        feed.watch(["t1"])
        feed.start()
        await until(lambda: "PING" in frames and bool(quotes))
        await feed.stop()
    assert feed.stats.total >= 2  # PONG frames count as liveness
    assert quotes[0].bid == Decimal("0.10")


async def test_watch_and_unwatch_after_connect_send_operations() -> None:
    frames: list[Any] = []

    async def handler(ws: ServerConnection) -> None:
        async for raw in ws:
            frames.append(json.loads(raw))

    async with fake_server(handler) as url:
        feed = market_feed(url, [])
        feed.watch(["a"])
        feed.start()
        await until(lambda: feed.stats.connected)
        feed.watch(["b", "a"])
        await until(lambda: {"assets_ids": ["b"], "operation": "subscribe"} in frames)
        feed.unwatch(["a"])
        await until(lambda: {"assets_ids": ["a"], "operation": "unsubscribe"} in frames)
        await feed.stop()
    assert feed.stats.subs == 1


async def test_reconnect_rebuilds_the_book_from_a_fresh_snapshot() -> None:
    connections: list[Any] = []
    quotes: list[Quote] = []
    states: list[tuple[str, bool]] = []

    async def handler(ws: ServerConnection) -> None:
        connections.append(json.loads(await ws.recv()))
        if len(connections) == 1:
            await ws.send(book("t1", [("0.30", "5")], [("0.35", "5")]))
            await asyncio.sleep(0.05)
            await ws.close()  # the server drops us
        else:
            await ws.send(book("t1", [("0.50", "5")], [("0.55", "5")]))
            await ws.wait_closed()

    async with fake_server(handler) as url:
        feed = market_feed(url, quotes, on_state=lambda n, up: states.append((n, up)))
        feed.watch(["t1"])
        feed.start()
        await until(lambda: len(quotes) >= 2)
        await feed.stop()
    assert len(connections) >= 2 and connections[1]["assets_ids"] == ["t1"]  # resubscribed on its own
    assert quotes[-1].bid == Decimal("0.50")  # nothing of the old book survived
    assert feed.stats.reconnects >= 1
    assert [up for _, up in states][:3] == [True, False, True]


async def test_silent_feed_is_dropped_and_reconnected() -> None:
    count = 0

    async def handler(ws: ServerConnection) -> None:
        nonlocal count
        count += 1
        await ws.recv()
        await ws.wait_closed()  # never answers anything

    async with fake_server(handler) as url:
        feed = market_feed(url, [], silence_limit=0.1, ping_seconds=10)
        feed.watch(["t1"])
        feed.start()
        await until(lambda: count >= 2)
        await feed.stop()


async def test_tick_size_change_is_recorded_and_book_is_kept() -> None:
    quotes: list[Quote] = []

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(book("t1", [("0.50", "5")], [("0.60", "5")]))
        await ws.send(json.dumps({"event_type": "tick_size_change", "asset_id": "t1", "new_tick_size": "0.001"}))
        await ws.send(json.dumps({"event_type": "last_trade_price", "asset_id": "t1", "price": "0.55"}))
        await ws.wait_closed()

    async with fake_server(handler) as url:
        feed = market_feed(url, quotes)
        feed.watch(["t1"])
        feed.start()
        await until(lambda: len(quotes) >= 2)
        await feed.stop()
    assert feed.tick_sizes == {"t1": "0.001"}
    assert (quotes[-1].bid, quotes[-1].ask, quotes[-1].last) == (Decimal("0.50"), Decimal("0.60"), Decimal("0.55"))


async def test_market_resolved_and_new_market_are_reported_and_unknown_assets_ignored() -> None:
    resolved: list[tuple[list[str], str | None]] = []
    new: list[dict[str, Any]] = []
    quotes: list[Quote] = []

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(book("other", [("0.1", "1")], []))  # not watched: ignored
        await ws.send(json.dumps({"event_type": "new_market", "id": "x"}))
        await ws.send(
            json.dumps({"event_type": "market_resolved", "assets_ids": ["t1", "t1n"], "winning_asset_id": "t1"})
        )
        await ws.wait_closed()

    async with fake_server(handler) as url:
        feed = market_feed(url, quotes, on_resolved=lambda ids, w: resolved.append((ids, w)), on_new_market=new.append)
        feed.watch(["t1"])
        feed.start()
        await until(lambda: bool(resolved))
        await feed.stop()
    assert resolved == [(["t1", "t1n"], "t1")]
    assert new == [{"event_type": "new_market", "id": "x"}]
    assert quotes == []


def test_bad_frames_do_not_raise() -> None:
    feed = PolymarketMarketFeed("ws://x", on_quote=lambda q: None)
    for raw in (
        "not json",
        "[]",
        "[1, 2]",
        '{"event_type": "book"}',
        b"PONG",
        '{"event_type": "price_change", "price_changes": [1]}',
    ):
        feed.handle(raw)


def test_backoff_has_jitter_and_a_cap() -> None:
    assert backoff_delay(0, base=1, cap=30, rand=lambda: 0.0) == 0.5
    assert backoff_delay(3, base=1, cap=30, rand=lambda: 1.0) == 8.0
    assert backoff_delay(20, base=1, cap=30, rand=lambda: 1.0) == 30.0


def test_feed_stats_rate_and_age() -> None:
    now = [0.0]
    stats = FeedStats(lambda: now[0])
    assert stats.age() is None
    for _ in range(20):
        stats.message()
    now[0] = 4.0
    assert stats.per_second() == 2.0 and stats.age() == 4.0
    assert set(stats.as_dict()) == {"connected", "reconnects", "msgs_per_s", "last_msg_age_s", "subs", "dropped"}


# ------------------------------------------------------------------------------------------ sports feed
async def test_sports_ping_pong_and_home_away_score_orientation() -> None:
    got: list[Any] = []
    pongs: list[str] = []

    async def handler(ws: ServerConnection) -> None:
        await ws.send("ping")
        pongs.append(str(await ws.recv()))
        await ws.send(
            json.dumps(
                {
                    "gameId": 9001,
                    "leagueAbbreviation": "epl",
                    "homeTeam": "Chelsea",
                    "awayTeam": "Arsenal",
                    "status": "InProgress",
                    "live": True,
                    "ended": False,
                    "score": "2-1",
                    "period": "2H",
                    "elapsed": "67",
                }
            )
        )
        await ws.wait_closed()

    async with fake_server(handler) as url:
        feed = PolymarketSportsFeed(url, on_game=got.append, backoff_base=0.01)
        feed.start()
        await until(lambda: bool(got))
        await feed.stop()
    assert pongs == ["pong"]
    state = got[0]
    assert (state.home, state.home_score, state.away, state.away_score) == (
        "Chelsea",
        2,
        "Arsenal",
        1,
    )  # "<home>-<away>"
    assert state.period_text == "2nd half" and state.live
    assert feed.find(9001) is state
    assert feed.find(None, "arsenal", "CHELSEA") is state  # joined by team names when the id is unknown
    assert feed.find(1234) is None


def test_periods_and_scores_that_are_not_simple() -> None:
    assert [period_label(p) for p in ("1H", "HT", "FT OT", "End 3", "2/4", "Weird")] == [
        "1st half",
        "Half-time",
        "Full time (extra time)",
        "End of period 3",
        "Period 2 of 4",
        "Weird",
    ]
    tennis = parse_game({"gameId": 5, "homeTeam": "A", "awayTeam": "B", "score": "6-4, 3-2"})
    assert tennis is not None and tennis.home_score is None and tennis.score_raw == "6-4, 3-2"
    assert parse_game({"homeTeam": "A"}) is None


def test_sports_games_are_bounded() -> None:
    feed = PolymarketSportsFeed("ws://x", on_game=lambda g: None, max_games=3)
    for i in range(10):
        feed.handle(json.dumps({"gameId": i, "homeTeam": "H", "awayTeam": "A"}))
    assert list(feed.games) == ["7", "8", "9"]


# ------------------------------------------------------------------------------------------ history
async def test_history_is_fetched_in_chunks_of_twenty_and_a_failed_chunk_is_skipped() -> None:
    calls: list[dict[str, Any]] = []

    async def post(url: str, *, json: Any) -> Any:
        calls.append(json)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return {"history": {t: [{"t": 20, "p": 0.5}, {"t": 10, "p": "0.25"}] for t in json["markets"]}}

    tokens = [f"t{i}" for i in range(45)]
    out = await fetch_history(post, tokens, "1w")
    assert [len(c["markets"]) for c in calls] == [20, 20, 5]
    assert {c["interval"] for c in calls} == {"1w"}
    assert set(out) == set(tokens[:20]) | set(tokens[40:])
    assert out["t0"] == [[10, "0.25"], [20, "0.5"]]  # oldest first, prices as Decimal strings
