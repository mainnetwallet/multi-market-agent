"""Kalshi and Limitless market feeds against local fake servers that speak the documented protocols, plus their charts.

They prove our side (framing, local book, gap handling, reconnect, units). They cannot prove the real servers
behave the same way: see WORKLOG.md (LIVE_VERIFIED=false) for what the owner must still run once.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from mm_agent.live.common import Quote
from mm_agent.live.history import (
    fetch_kalshi_history,
    fetch_limitless_history,
    kalshi_points,
    limitless_points,
)
from mm_agent.live.kalshi_ws import KalshiMarketFeed
from mm_agent.live.limitless_ws import LimitlessMarketFeed

Handler = Callable[[ServerConnection], Awaitable[None]]
SECRET = "SECRET-SIGNATURE-VALUE"


@asynccontextmanager
async def fake_server(handler: Handler) -> AsyncIterator[str]:
    async with serve(handler, "127.0.0.1", 0) as server:
        yield f"ws://127.0.0.1:{next(iter(server.sockets)).getsockname()[1]}"


async def until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    async def wait() -> None:
        while not pred():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


# ------------------------------------------------------------------------------------------------ Kalshi
def snap(sid: int, seq: int, ticker: str, yes: list[list[str]], no: list[list[str]]) -> str:
    return json.dumps(
        {
            "type": "orderbook_snapshot",
            "sid": sid,
            "seq": seq,
            "msg": {"market_ticker": ticker, "yes_dollars_fp": yes, "no_dollars_fp": no},
        }
    )


def delta(sid: int, seq: int, ticker: str, side: str, price: str, change: str) -> str:
    return json.dumps(
        {
            "type": "orderbook_delta",
            "sid": sid,
            "seq": seq,
            "msg": {"market_ticker": ticker, "price_dollars": price, "delta_fp": change, "side": side},
        }
    )


class KalshiServer:
    """Acknowledges every subscribe with a ``sid`` and records what the client sends."""

    def __init__(self, script: Callable[[ServerConnection, dict[str, int]], Awaitable[None]] | None = None) -> None:
        self.frames: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.script = script
        self.connections = 0

    async def __call__(self, ws: ServerConnection) -> None:
        self.connections += 1
        self.headers.append({k.lower(): v for k, v in ws.request.headers.items()})
        sids: dict[str, int] = {}
        tasks: list[asyncio.Task[None]] = []
        try:
            async for raw in ws:
                msg = json.loads(raw)
                self.frames.append(msg)
                if msg["cmd"] != "subscribe":
                    continue
                channel = msg["params"]["channels"][0]
                sids[channel] = len(sids) + 1
                await ws.send(
                    json.dumps(
                        {"id": msg["id"], "type": "subscribed", "msg": {"channel": channel, "sid": sids[channel]}}
                    )
                )
                if channel == "orderbook_delta" and self.script is not None:
                    # a task, not an await: the script may wait for frames that only this receive loop can read
                    tasks.append(asyncio.create_task(self.script(ws, sids)))
        except Exception:
            return
        finally:
            for task in tasks:
                task.cancel()

    def commands(self, cmd: str) -> list[dict[str, Any]]:
        return [f for f in self.frames if f["cmd"] == cmd]


def kalshi_feed(url: str, quotes: list[Quote], **kw: Any) -> KalshiMarketFeed:
    kw.setdefault("headers", lambda: {"KALSHI-ACCESS-SIGNATURE": SECRET, "KALSHI-ACCESS-KEY": "key-id"})
    headers = kw.pop("headers")
    return KalshiMarketFeed(url, headers, on_quote=quotes.append, backoff_base=0.01, backoff_cap=0.02, **kw)


async def test_kalshi_subscribes_signed_builds_a_book_and_applies_deltas() -> None:
    async def script(ws: ServerConnection, sids: dict[str, int]) -> None:
        sid = sids["orderbook_delta"]
        # YES bids 0.60/0.59, NO bids 0.38: the YES ask is 1 - 0.38 = 0.62
        await ws.send(snap(sid, 1, "KX-T1", [["0.60", "100.00"], ["0.59", "50.00"]], [["0.38", "80.00"]]))
        await ws.send(delta(sid, 2, "KX-T1", "yes", "0.60", "-100.00"))  # the best bid disappears
        await ws.send(delta(sid, 3, "KX-T1", "no", "0.39", "10.00"))  # a better NO bid lowers the ask

    quotes: list[Quote] = []
    server = KalshiServer(script)
    async with fake_server(server) as url:
        feed = kalshi_feed(url, quotes)
        feed.watch(["KX-T1"])
        feed.start()
        await until(lambda: len(quotes) >= 3)
        await feed.stop()
    assert [(q.bid, q.ask) for q in quotes[:3]] == [
        (Decimal("0.60"), Decimal("0.62")),
        (Decimal("0.59"), Decimal("0.62")),
        (Decimal("0.59"), Decimal("0.61")),
    ]
    subs = server.commands("subscribe")
    assert [s["params"]["channels"] for s in subs] == [
        ["ticker"],
        ["trade"],
        ["orderbook_delta"],
        ["market_lifecycle_v2"],
    ]
    assert all(s["params"].get("market_tickers") == ["KX-T1"] for s in subs[:3])
    assert "market_tickers" not in subs[3]["params"]  # lifecycle is exchange-wide and filtered locally
    assert server.headers[0]["kalshi-access-key"] == "key-id" and server.headers[0]["kalshi-access-signature"] == SECRET


async def test_kalshi_secrets_never_travel_in_payloads_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    quotes: list[Quote] = []
    server = KalshiServer()
    async with fake_server(server) as url:
        feed = kalshi_feed(url, quotes)
        feed.watch(["KX-T1"])
        feed.start()
        await until(lambda: len(server.frames) >= 4)
        await feed.stop()
    assert SECRET not in json.dumps(server.frames)
    # the websockets library itself logs every handshake header at DEBUG; what the app logs must stay clean
    ours = [r.getMessage() for r in caplog.records if not r.name.startswith("websockets")]
    assert SECRET not in " ".join(ours)


async def test_kalshi_ticker_channel_gives_top_of_book_before_any_snapshot() -> None:
    async def script(ws: ServerConnection, sids: dict[str, int]) -> None:
        await ws.send(
            json.dumps(
                {
                    "type": "ticker",
                    "sid": 1,
                    "msg": {
                        "market_ticker": "KX-T1",
                        "yes_bid_dollars": "0.55",
                        "yes_ask_dollars": "0.57",
                        "price_dollars": "0.56",
                        "ts": 1700000000,
                    },
                }
            )
        )
        await ws.send(
            json.dumps({"type": "trade", "sid": 2, "msg": {"market_ticker": "KX-T1", "yes_price_dollars": "0.58"}})
        )

    quotes: list[Quote] = []
    async with fake_server(KalshiServer(script)) as url:
        feed = kalshi_feed(url, quotes)
        feed.watch(["KX-T1"])
        feed.start()
        await until(lambda: len(quotes) >= 2)
        await feed.stop()
    assert (quotes[0].bid, quotes[0].ask, quotes[0].last, quotes[0].source_ts) == (
        Decimal("0.55"),
        Decimal("0.57"),
        Decimal("0.56"),
        "1700000000",
    )
    assert quotes[1].last == Decimal("0.58") and quotes[1].bid == Decimal("0.55")  # a trade keeps the known bid/ask


async def test_kalshi_a_gap_in_seq_drops_the_book_and_asks_for_a_snapshot() -> None:
    async def script(ws: ServerConnection, sids: dict[str, int]) -> None:
        sid = sids["orderbook_delta"]
        await ws.send(snap(sid, 1, "KX-T1", [["0.60", "10.00"]], [["0.38", "10.00"]]))
        await ws.send(delta(sid, 3, "KX-T1", "yes", "0.61", "5.00"))  # seq 2 never arrived
        # the server answers the get_snapshot request below with a fresh snapshot
        while not any(f["params"].get("action") == "get_snapshot" for f in server.commands("update_subscription")):
            await asyncio.sleep(0.01)
        await ws.send(snap(sid, 4, "KX-T1", [["0.62", "10.00"]], [["0.36", "10.00"]]))

    quotes: list[Quote] = []
    server = KalshiServer(script)
    async with fake_server(server) as url:
        feed = kalshi_feed(url, quotes)
        feed.watch(["KX-T1"])
        feed.start()
        await until(lambda: len(quotes) >= 2)
        await feed.stop()
    assert [(q.bid, q.ask) for q in quotes] == [(Decimal("0.60"), Decimal("0.62")), (Decimal("0.62"), Decimal("0.64"))]
    (request,) = [f for f in server.commands("update_subscription") if f["params"]["action"] == "get_snapshot"]
    assert request["params"]["sids"] == [3] and request["params"]["market_tickers"] == ["KX-T1"]


async def test_kalshi_markets_added_and_removed_while_connected() -> None:
    quotes: list[Quote] = []
    server = KalshiServer()
    async with fake_server(server) as url:
        feed = kalshi_feed(url, quotes)
        feed.watch(["KX-A"])
        feed.start()
        await until(lambda: feed.stats.connected and len(server.commands("subscribe")) == 4)
        feed.watch(["KX-B"])
        feed.unwatch(["KX-A"])
        await until(lambda: len(server.commands("update_subscription")) == 6)
        await feed.stop()
    updates = server.commands("update_subscription")
    added = [u for u in updates if u["params"]["action"] == "add_markets"]
    removed = [u for u in updates if u["params"]["action"] == "delete_markets"]
    assert sorted(u["params"]["sids"][0] for u in added) == [1, 2, 3]  # ticker, trade and orderbook_delta
    assert all(u["params"]["market_tickers"] == ["KX-B"] for u in added)
    assert all(u["params"]["market_tickers"] == ["KX-A"] for u in removed)
    assert feed.stats.subs == 1


async def test_kalshi_units_legacy_cents_only_as_whole_numbers_and_crossed_quotes_are_dropped() -> None:
    feed = KalshiMarketFeed("ws://x", dict, on_quote=(quotes := []).append)  # type: ignore[arg-type]
    feed._assets.add("KX-T1")
    tick = {"type": "ticker", "msg": {"market_ticker": "KX-T1"}}

    def send(**fields: Any) -> None:
        feed.handle(json.dumps({**tick, "msg": {**tick["msg"], **fields}}))  # type: ignore[dict-item]

    send(yes_bid=55, yes_ask=57, price=56)  # documented legacy cents: whole numbers 0..100
    assert (quotes[-1].bid, quotes[-1].ask, quotes[-1].last) == (Decimal("0.55"), Decimal("0.57"), Decimal("0.56"))
    send(yes_bid=55.5, yes_ask=57)  # a fractional "cent" is not something we guess about
    assert (quotes[-1].bid, quotes[-1].ask) == (None, Decimal("0.57"))
    send(yes_bid=155, yes_ask=57)  # out of range
    assert quotes[-1].bid is None
    send(yes_bid_dollars="1.40", yes_ask_dollars="0.57")  # dollars above 1
    assert quotes[-1].bid is None
    count = len(quotes)
    send(yes_bid_dollars="0.70", yes_ask_dollars="0.60")  # crossed
    assert len(quotes) == count and feed.ignored == 1
    feed.handle("not json")
    feed.handle(json.dumps(["not", "a", "dict"]))
    feed.handle(json.dumps({"type": "ticker", "msg": {"market_ticker": "UNWATCHED", "yes_bid_dollars": "0.5"}}))
    assert len(quotes) == count


async def test_kalshi_lifecycle_settlement_is_reported_and_its_subscribe_error_is_not_fatal() -> None:
    settled: list[tuple[str, bool | None]] = []

    class Server(KalshiServer):
        async def __call__(self, ws: ServerConnection) -> None:
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    self.frames.append(msg)
                    channel = msg["params"]["channels"][0]
                    if channel == "market_lifecycle_v2":
                        await ws.send(json.dumps({"id": msg["id"], "type": "error", "msg": {"code": 8, "msg": "nope"}}))
                    else:
                        await ws.send(
                            json.dumps({"id": msg["id"], "type": "subscribed", "msg": {"channel": channel, "sid": 1}})
                        )
                await ws.wait_closed()
            except Exception:
                return

    quotes: list[Quote] = []
    async with fake_server(Server()) as url:
        feed = kalshi_feed(url, quotes, on_settled=lambda t, won: settled.append((t, won)))
        feed.watch(["KX-T1"])
        feed.start()
        await until(lambda: feed.stats.connected and feed.stats.total >= 3)
        assert feed.stats.reconnects == 0  # the lifecycle error did not end the session
        feed.handle(
            json.dumps(
                {
                    "type": "market_lifecycle_v2",
                    "msg": {"market_ticker": "KX-T1", "event_type": "settled", "result": "yes"},
                }
            )
        )
        feed.handle(
            json.dumps(
                {
                    "type": "market_lifecycle_v2",
                    "msg": {"market_ticker": "KX-T1", "event_type": "activated", "result": ""},
                }
            )
        )
        feed.handle(
            json.dumps(
                {
                    "type": "market_lifecycle_v2",
                    "msg": {"market_ticker": "OTHER", "event_type": "settled", "result": "no"},
                }
            )
        )
        await feed.stop()
    assert settled == [("KX-T1", True)]


async def test_kalshi_refused_data_subscription_reconnects_with_fresh_headers_and_clears_books() -> None:
    calls: list[int] = []

    def headers() -> dict[str, str]:
        calls.append(1)
        return {"KALSHI-ACCESS-KEY": "k"}

    attempts = 0

    async def script(ws: ServerConnection, sids: dict[str, int]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await ws.send(snap(sids["orderbook_delta"], 1, "KX-T1", [["0.60", "10.00"]], [["0.38", "10.00"]]))
            await ws.send(json.dumps({"id": 99, "type": "error", "msg": {"code": 5, "msg": "bad"}}))
        else:
            await ws.send(snap(sids["orderbook_delta"], 1, "KX-T1", [["0.50", "10.00"]], [["0.40", "10.00"]]))

    quotes: list[Quote] = []
    state: list[tuple[str, bool]] = []
    server = KalshiServer(script)
    async with fake_server(server) as url:
        feed = kalshi_feed(url, quotes, headers=headers, on_state=lambda n, c: state.append((n, c)))
        feed.watch(["KX-T1"])
        feed.start()
        await until(lambda: len(quotes) >= 2)
        await feed.stop()
    assert len(calls) >= 2 and server.connections >= 2  # signed again for the second attempt
    assert quotes[0].bid == Decimal("0.60") and quotes[1].bid == Decimal("0.50")
    assert ("kalshi_ws", True) in state and ("kalshi_ws", False) in state
    assert feed.stats.reconnects >= 1


async def test_kalshi_ping_keeps_the_link_marked_alive_without_counting_as_data() -> None:
    quotes: list[Quote] = []
    server = KalshiServer()
    async with fake_server(server) as url:
        feed = kalshi_feed(url, quotes, heartbeat_seconds=0.05)
        feed.watch(["KX-T1"])
        feed.start()
        await until(lambda: feed.stats.connected and feed.stats.total >= 4)  # the four subscribe acks
        total, last = feed.stats.total, feed.stats.last_msg
        await until(lambda: (feed.stats.last_msg or 0) > (last or 0) + 0.1)
        assert feed.stats.total == total
        await feed.stop()


async def test_kalshi_stays_disconnected_until_something_is_watched() -> None:
    server = KalshiServer()
    async with fake_server(server) as url:
        feed = kalshi_feed(url, [])
        feed.start()
        await asyncio.sleep(0.1)
        assert server.connections == 0 and not feed.stats.connected
        feed.watch(["KX-T1"])
        await until(lambda: feed.stats.connected)
        await feed.stop()


# --------------------------------------------------------------------------------------------- Limitless
class LimitlessServer:
    """Engine.IO open, namespace ack, then records ``subscribe_*`` events; ``script`` may push events."""

    def __init__(self, script: Callable[[ServerConnection, LimitlessServer], Awaitable[None]] | None = None) -> None:
        self.events: list[list[Any]] = []
        self.headers: list[dict[str, str]] = []
        self.pongs = 0
        self.script = script
        self.connections = 0

    async def __call__(self, ws: ServerConnection) -> None:
        self.connections += 1
        self.headers.append({k.lower(): v for k, v in ws.request.headers.items()})
        try:
            await ws.send('0{"sid":"abc","upgrades":[],"pingInterval":25000,"pingTimeout":20000}')
            async for raw in ws:
                text = raw if isinstance(raw, str) else raw.decode()
                if text == "40/markets,":
                    await ws.send('40/markets,{"sid":"xyz"}')
                    await ws.send("2")  # an Engine.IO ping, answered with 3
                elif text == "3":
                    self.pongs += 1
                elif text.startswith("42/markets,"):
                    payload = json.loads(text[len("42/markets,") :])
                    self.events.append(payload)
                    if payload[0] == "subscribe_market_prices" and self.script is not None:
                        await self.script(ws, self)
        except Exception:
            return

    def subscriptions(self) -> list[list[str]]:
        return [e[1]["marketSlugs"] for e in self.events if e[0] == "subscribe_market_prices"]


def event(name: str, data: Any) -> str:
    return "42/markets," + json.dumps([name, data])


def limitless_feed(url: str, quotes: list[Quote], **kw: Any) -> LimitlessMarketFeed:
    return LimitlessMarketFeed(url, on_quote=quotes.append, backoff_base=0.01, backoff_cap=0.02, **kw)


async def test_limitless_subscribes_publicly_and_turns_book_updates_into_quotes() -> None:
    async def script(ws: ServerConnection, srv: LimitlessServer) -> None:
        await ws.send(
            event(
                "orderbookUpdate",
                {
                    "marketSlug": "mkt-a",
                    "orderbook": {
                        "bids": [{"price": "0.44", "size": "10"}, {"price": "0.43", "size": "5"}],
                        "asks": [
                            {"price": "0.47", "size": "12"},
                            {"price": "0.46", "size": "0"},
                            {"price": 0.48, "size": 3},
                        ],
                    },
                    "timestamp": 1700000000,
                },
            )
        )

    quotes: list[Quote] = []
    server = LimitlessServer(script)
    async with fake_server(server) as url:
        feed = limitless_feed(url, quotes)
        feed.watch(["mkt-a"])
        feed.start()
        await until(lambda: len(quotes) >= 1 and server.pongs >= 1)
        await feed.stop()
    assert (quotes[0].asset, quotes[0].bid, quotes[0].ask, quotes[0].source_ts) == (
        "mkt-a",
        Decimal("0.44"),
        Decimal("0.47"),  # the zero-size 0.46 level does not count
        "1700000000",
    )
    assert server.subscriptions() == [["mkt-a"]]
    assert any(e[0] == "subscribe_market_lifecycle" for e in server.events)
    assert not any(k.startswith("lmts-") for k in server.headers[0])  # the price stream is public: no credentials


async def test_limitless_resubscribes_with_the_whole_list_when_markets_change() -> None:
    server = LimitlessServer()
    async with fake_server(server) as url:
        feed = limitless_feed(url, [])
        feed.watch(["mkt-a"])
        feed.start()
        await until(lambda: server.subscriptions() == [["mkt-a"]])
        feed.watch(["mkt-b"])
        await until(lambda: server.subscriptions() == [["mkt-a"], ["mkt-a", "mkt-b"]])
        feed.unwatch(["mkt-a"])
        await until(lambda: len(server.subscriptions()) == 3)
        await feed.stop()
    assert server.subscriptions()[-1] == ["mkt-b"]
    assert sum(1 for e in server.events if e[0] == "subscribe_market_lifecycle") == 1


async def test_limitless_amm_price_is_a_single_quoted_price_and_bad_values_are_ignored() -> None:
    quotes: list[Quote] = []
    feed = LimitlessMarketFeed("ws://x", on_quote=quotes.append)
    feed._assets.add("mkt-a")
    feed.handle(event("newPriceData", {"marketSlug": "mkt-a", "updatedPrices": {"yes": 0.62, "no": 0.38}}))
    assert (quotes[-1].quoted, quotes[-1].bid, quotes[-1].ask) == (Decimal("0.62"), None, None)
    count = len(quotes)
    feed.handle(event("newPriceData", {"marketSlug": "mkt-a", "updatedPrices": {"yes": 62}}))  # percent: never rescaled
    feed.handle(event("newPriceData", {"marketAddress": "0xabc", "updatedPrices": {"yes": 0.5}}))  # names no market
    feed.handle(event("newPriceData", {"marketSlug": "other", "updatedPrices": {"yes": 0.5}}))
    feed.handle(
        event(
            "orderbookUpdate",
            {
                "marketSlug": "mkt-a",
                "orderbook": {"bids": [{"price": "0.7", "size": "1"}], "asks": [{"price": "0.6", "size": "1"}]},
            },
        )
    )  # crossed
    feed.handle(
        event("orderbookUpdate", {"marketSlug": "mkt-a", "orderbook": {"bids": [{"price": "55", "size": "1"}]}})
    )
    feed.handle("42/markets,not json")
    feed.handle("42/markets,[]")
    assert len(quotes) == count + 1  # only the book with no valid level (55 is out of range) still emits an empty quote
    assert (quotes[-1].bid, quotes[-1].ask) == (None, None)
    # counted: percent price, update naming no market, crossed book; an unwatched market is skipped silently
    assert feed.ignored == 3


async def test_limitless_lifecycle_settlement_and_error_events() -> None:
    settled: list[tuple[str, bool | None]] = []
    feed = LimitlessMarketFeed("ws://x", on_quote=lambda q: None, on_settled=lambda s, w: settled.append((s, w)))
    feed._assets.update({"mkt-a", "mkt-b"})
    feed.handle(event("marketLifecycle", {"marketSlug": "mkt-a", "status": "resolved", "winningOutcomeIndex": 0}))
    feed.handle(event("marketLifecycle", {"marketSlug": "mkt-b", "status": "expired"}))
    feed.handle(event("marketLifecycle", {"marketSlug": "mkt-a", "status": "funded"}))
    assert settled == [("mkt-a", True), ("mkt-b", None)]
    feed.handle(event("exception", {"message": "subscribe_market_lifecycle is not allowed"}))  # a bonus channel
    with pytest.raises(Exception, match="server error"):
        feed.handle(event("exception", {"message": "invalid marketSlugs"}))


async def test_limitless_reconnects_and_subscribes_again_from_scratch() -> None:
    attempts = 0

    async def script(ws: ServerConnection, srv: LimitlessServer) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await ws.close()

    quotes: list[Quote] = []
    state: list[tuple[str, bool]] = []
    server = LimitlessServer(script)
    async with fake_server(server) as url:
        feed = limitless_feed(url, quotes, on_state=lambda n, c: state.append((n, c)))
        feed.watch(["mkt-a"])
        feed.start()
        await until(lambda: len(server.subscriptions()) >= 2)
        await feed.stop()
    assert server.connections >= 2 and feed.stats.reconnects >= 1
    assert ("limitless_ws", True) in state and ("limitless_ws", False) in state


async def test_limitless_refused_namespace_is_a_failed_session_not_a_silent_one() -> None:
    class Refuse(LimitlessServer):
        async def __call__(self, ws: ServerConnection) -> None:
            self.connections += 1
            try:
                await ws.send('0{"sid":"abc","pingInterval":25000,"pingTimeout":20000}')
                async for raw in ws:
                    if raw == "40/markets,":
                        await ws.send('44/markets,{"message":"forbidden"}')
            except Exception:
                return

    server = Refuse()
    async with fake_server(server) as url:
        feed = limitless_feed(url, [])
        feed.watch(["mkt-a"])
        feed.start()
        await until(lambda: server.connections >= 2)  # it reconnected: the refusal ended the session
        await feed.stop()
    assert not feed.stats.connected


# ------------------------------------------------------------------------------------------------ charts
def test_kalshi_points_use_the_midpoint_and_never_guess_cents() -> None:
    candles = [
        {"end_period_ts": 200, "yes_bid": {"close_dollars": "0.50"}, "yes_ask": {"close_dollars": "0.54"}},
        {"end_period_ts": 100, "price": {"close_dollars": "0.40"}},  # no quotes that period: the last trade
        {"end_period_ts": 300, "price": {"close": 55}},  # whole cents: refused
        {"end_period_ts": 400, "price": {"close": "0.61"}},  # a decimal string is a probability
        {"end_period_ts": 500, "yes_bid": {"close_dollars": "0.70"}, "yes_ask": {"close_dollars": "0.60"}},  # crossed
        {"timestamp": "x", "price": {"close_dollars": "0.5"}},
        "junk",
    ]
    assert kalshi_points(candles) == [[100, "0.4"], [200, "0.52"], [400, "0.61"]]


async def test_kalshi_history_batches_tickers_and_uses_the_range() -> None:
    calls: list[dict[str, Any]] = []

    async def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
        calls.append({"url": url, **(params or {})})
        if len(calls) == 2:
            raise RuntimeError("boom")  # one failing batch must not lose the others
        names = params["market_tickers"].split(",") if params else []
        return {
            "markets": [
                {"market_ticker": n, "candlesticks": [{"end_period_ts": 1000, "price": {"close_dollars": "0.3"}}]}
                for n in names
            ]
        }

    tickers = [f"KX-{i}" for i in range(250)] + ["../bad"]
    out = await fetch_kalshi_history(
        get_json, "https://api.example/trade-api/v2", tickers, "1W", now=lambda: 10_000_000
    )
    assert len(calls) == 3 and len(calls[0]["market_tickers"].split(",")) == 100
    assert calls[0]["url"] == "https://api.example/trade-api/v2/markets/candlesticks"
    assert (calls[0]["period_interval"], calls[0]["end_ts"] - calls[0]["start_ts"]) == (60, 7 * 86400)
    assert set(out) == {f"KX-{i}" for i in range(100)} | {f"KX-{i}" for i in range(200, 250)}
    assert "../bad" not in json.dumps(calls)
    again = await fetch_kalshi_history(get_json, "https://x", ["KX-1"], "nonsense", now=lambda: 10_000_000)
    assert again["KX-1"] == [[1000, "0.3"]] and calls[-1]["period_interval"] == 1  # unknown range -> 1D


def test_limitless_points_sort_oldest_first_and_reject_percent() -> None:
    newest_first = {
        "prices": [
            {"timestamp": 1_700_000_300_000, "price": "0.55"},  # milliseconds
            {"timestamp": 1_700_000_200, "price": 0.5},
            {"timestamp": 1_700_000_100, "price": 55},  # percent: dropped, not rescaled
            [1_700_000_050, "0.45"],
            {"t": "x", "price": 0.5},
        ]
    }
    assert limitless_points(newest_first) == [[1_700_000_050, "0.45"], [1_700_000_200, "0.5"], [1_700_000_300, "0.55"]]
    assert limitless_points([{"time": 5, "value": 0.2}]) == [[5, "0.2"]]
    assert limitless_points({"unknown": 1}) == [] and limitless_points("junk") == []


async def test_limitless_history_cuts_to_the_range_and_tries_interval_only_when_the_plain_call_is_empty() -> None:
    calls: list[tuple[str, Any]] = []
    now = 1_700_100_000

    async def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
        calls.append((url, params))
        if "slow" in url and params is None:
            return {"prices": []}  # the plain call gives nothing: ask again with an interval
        if "down" in url:
            raise RuntimeError("500")
        return [{"timestamp": now - 7200, "price": 0.4}, {"timestamp": now - 600, "price": 0.5}]

    out = await fetch_limitless_history(
        get_json, "https://api.example", ["fast", "slow", "down", "../x"], "1H", now=lambda: now
    )
    assert out == {"fast": [[now - 600, "0.5"]], "slow": [[now - 600, "0.5"]]}  # two hours ago is outside 1H
    urls = [(u.rsplit("/", 2)[-2], p) for u, p in calls]
    assert ("fast", None) in urls and ("slow", {"interval": "1h"}) in urls
    assert not any(u == "x" and "../" in str(p) for u, p in urls) and not any("../x" in u for u, _ in calls)
