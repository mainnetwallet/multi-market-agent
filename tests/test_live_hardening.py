"""P5 hardening: abusive clients, malformed frames, silent or flapping feeds, per-user socket cap (no network)."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from mm_agent.live.hub import ClientSink
from tests.conftest import Harness
from tests.test_live_hub import FEED, drain, make_hub, q, stream_client, sub


# ------------------------------------------------------------------------------------------ abusive clients
async def test_flooding_bad_frames_without_reading_closes_the_client() -> None:
    hub, _, _, _ = make_hub()
    sink = hub.connect("flood")
    for _ in range(2000):  # nothing is ever read from the sink
        await hub.handle(sink, {"op": "nope"})
    assert sink.closed and sink.overflowed and sink.pending() <= 512
    assert await sink.get() is None  # the sender task ends, the socket closes with 4408
    hub.disconnect(sink)
    assert hub.metrics()["clients"] == 0


async def test_event_lookups_are_budgeted_per_client() -> None:
    hub, loader, clock, _ = make_hub()
    sink = hub.connect("spam")
    for i in range(hub.max_loads + 5):
        await hub.handle(sink, {"op": "subscribe", "venue": "polymarket", "id": "missing"})
        # same id again is a repeat lookup: it still costs the venue a call
        drain(sink)
        await hub.handle(sink, {"op": "subscribe", "venue": "polymarket", "id": f"nope-{i}"})
    codes = [f.get("code") for f in drain(sink)]
    assert "rate_limited" in codes
    assert len(loader.calls) <= hub.max_loads * 2 + 1  # upstream calls are bounded, not one per frame
    clock.t += 61  # the window moves on
    await hub.handle(sink, {"op": "subscribe", "venue": "polymarket", "id": "cand"})
    assert drain(sink)[-1]["type"] == "snapshot"


async def test_known_events_do_not_spend_the_lookup_budget() -> None:
    hub, loader, _, _ = make_hub()
    sinks = [hub.connect(f"c{i}") for i in range(hub.max_loads + 10)]
    for s in sinks:  # many clients on one event: one lookup in total, nobody is limited
        await sub(hub, s)
    assert len(loader.calls) == 1
    assert all(drain(s)[-1]["type"] == "snapshot" for s in sinks)


async def test_lock_table_does_not_grow_with_invented_ids() -> None:
    hub, _, _, _ = make_hub(max_loads_per_minute=10_000)
    sink = hub.connect("x")
    await asyncio.gather(
        *(hub.handle(sink, {"op": "subscribe", "venue": "polymarket", "id": "missing"}) for _ in range(20))
    )
    for i in range(300):
        await hub.handle(sink, {"op": "subscribe", "venue": "kalshi", "id": f"ghost{i}"})
        drain(sink)
    assert hub._locks == {}


@pytest.mark.parametrize(
    "frame",
    [
        {},
        {"op": None},
        {"op": 7},
        {"op": ["subscribe"]},
        {"op": "subscribe"},
        {"op": "subscribe", "venue": "polymarket"},
        {"op": "subscribe", "venue": "polymarket", "id": ""},
        {"op": "subscribe", "venue": "polymarket", "id": "a b"},
        {"op": "subscribe", "venue": "polymarket", "id": "../../etc/passwd"},
        {"op": "subscribe", "venue": "polymarket", "id": "x" * 5000},
        {"op": "subscribe", "venue": {"a": 1}, "id": "cand"},
        {"op": "subscribe", "venue": "evil", "id": "cand"},
        {"op": "subscribe", "topic": "account", "venue": "polymarket", "id": "cand"},
        {"op": "subscribe", "venue": "polymarket", "id": "cand", "range": {"x": 1}},
        {"op": "snapshot", "venue": "polymarket", "id": "cand"},
        {"op": "unsubscribe", "venue": "polymarket", "id": "never-subscribed"},
    ],
)
async def test_malformed_frames_get_an_error_not_a_crash(frame: dict[str, Any]) -> None:
    hub, _, _, _ = make_hub()
    sink = hub.connect("m")
    await hub.handle(sink, frame)
    out = drain(sink)
    assert all(f["type"] in {"error", "snapshot"} for f in out)
    assert hub.metrics()["events"] <= 1
    hub.disconnect(sink)


# ------------------------------------------------------------------------------------------ feed drills
async def test_silent_feed_flags_stale_then_recovers_on_the_next_quote() -> None:
    hub, _, clock, feed = make_hub()
    assert feed is not None
    sink = hub.connect("s")
    await sub(hub, sink)
    drain(sink)
    feed.stats.message()
    hub.on_quote(q("c11", "0.50", "0.54"))
    clock.t += 1
    hub.flush()
    drain(sink)
    clock.t += hub.stale_after + 5  # connected but not one message for longer than the limit
    hub._check_stale()
    flagged = drain(sink)
    assert [f["stale"] for f in flagged if f["type"] == "status"] == [True]
    feed.stats.message()  # the feed speaks again
    hub.on_quote(q("c11", "0.51", "0.55"))
    back = drain(sink)
    assert [f["stale"] for f in back if f["type"] == "status"] == [False]
    snap = [f for f in back if f["type"] == "snapshot"][-1]  # recovery sends a fresh snapshot
    assert snap["view"]["freshness"]["stale"] is False


async def test_reconnect_storm_stays_bounded_and_ends_consistent() -> None:
    hub, _, clock, feed = make_hub()
    assert feed is not None
    reader = hub.connect("reads")
    idle = hub.connect("never-reads")
    await sub(hub, reader)
    await sub(hub, idle)
    drain(reader)
    biggest = 0
    for i in range(3000):
        hub.on_feed_state(FEED, i % 2 == 1)  # down, up, down, up ...
        if i % 3 == 0:
            feed.stats.message()
            hub.on_quote(q("c11", "0.50", f"0.5{i % 9}"))
        if i % 50 == 0:
            clock.t += 1
            hub.flush()
            drain(reader)
        biggest = max(biggest, reader.pending(), idle.pending())
    assert biggest <= 512  # flapping status frames are coalesced, nothing grows with the storm
    assert idle.pending() <= 8  # even a client that never reads holds a handful of frames, not thousands
    hub.on_feed_state(FEED, True)
    feed.stats.message()
    hub.on_quote(q("c11", "0.50", "0.54"))
    clock.t += 1
    hub.flush()
    frames = drain(reader)
    assert not any(f["type"] == "status" and f["stale"] for f in frames[-1:])
    assert hub.metrics()["events"] == 1 and hub.metrics()["stale_events"] == 0


async def test_a_slow_reader_is_dropped_by_the_sweep_not_buffered_forever() -> None:
    hub, _, clock, _ = make_hub()
    sink = hub.connect("slow")
    await sub(hub, sink)
    clock.t += hub.client_idle + 1
    hub.sweep(clock.t)
    assert sink.closed and hub.metrics()["clients"] == 0


# ------------------------------------------------------------------------------------------ sockets per account
def test_one_account_cannot_hold_unlimited_stream_sockets(harness: Harness) -> None:
    hub, _, _, _ = make_hub()
    with stream_client(harness, hub) as client:
        assert (
            client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"}).status_code == 200
        )
        with ExitStack() as stack:
            socks = [stack.enter_context(client.websocket_connect("/v1/stream")) for _ in range(8)]
            assert all(s.receive_json()["type"] == "hello" for s in socks)
            with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/v1/stream"):
                pass
            assert exc.value.code == 4429


def test_error_frames_never_carry_secrets_or_internals(harness: Harness) -> None:
    hub, _, _, _ = make_hub()
    with stream_client(harness, hub) as client:
        client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
        with client.websocket_connect("/v1/stream") as ws:
            ws.receive_json()
            for frame in ({"op": "x"}, {"op": "subscribe", "venue": "polymarket", "id": "missing"}):
                ws.send_json(frame)
                text = str(ws.receive_json())
                assert "test-password" not in text and "Traceback" not in text and "token" not in text.lower()


def test_sink_type_is_exported() -> None:
    assert ClientSink.__name__ == "ClientSink"


# ------------------------------------------------------------------------------------------ venue 429
class RateLimited(Exception):
    def __init__(self, retry_after: float | None) -> None:
        super().__init__("429")
        self.retry_after = retry_after


async def test_venue_429_pauses_rest_refresh_for_the_retry_after_then_resumes() -> None:
    hub, loader, clock, _ = make_hub(rest_per_tick=10)
    sink = hub.connect("r")
    await sub(hub, sink)  # first load works
    hub.on_feed_state(FEED, False)  # the feed drops: the event is stale and the hub starts polling REST
    base = len(loader.calls)
    real = loader.view
    loader.view = None  # type: ignore[assignment]

    async def failing(venue: str, ident: str) -> Any:
        loader.calls.append((venue, ident))
        raise RateLimited(20.0)

    hub._load_view = failing  # type: ignore[method-assign]
    clock.t += 3
    await hub._poll_rest(clock.t)
    assert len(loader.calls) == base + 1  # one try, then the venue is left alone
    for _ in range(9):  # 18 s later, polled every 2 s: nothing more is sent to the venue
        clock.t += 2
        await hub._poll_rest(clock.t)
    assert len(loader.calls) == base + 1
    assert hub.metrics()["events"] == 1  # still followed, still flagged stale, no crash
    clock.t += 5  # past the 20 s the venue asked for

    async def ok(venue: str, ident: str) -> Any:
        loader.calls.append((venue, ident))
        return real

    hub._load_view = ok  # type: ignore[method-assign]
    await hub._poll_rest(clock.t)
    assert len(loader.calls) == base + 2 and hub._rest_block == {}


async def test_repeated_failures_back_off_without_a_retry_after() -> None:
    hub, loader, clock, _ = make_hub()
    sink = hub.connect("r")
    await sub(hub, sink)
    hub.on_feed_state(FEED, False)

    async def boom(venue: str, ident: str) -> Any:
        loader.calls.append((venue, ident))
        raise RuntimeError("down")

    hub._load_view = boom  # type: ignore[method-assign]
    start = len(loader.calls)
    for _ in range(120):  # four minutes at the 2 s tick: 120 calls without a backoff
        clock.t += 2
        await hub._poll_rest(clock.t)
    assert len(loader.calls) - start <= 12  # 4 s, 8 s, 16 s, then every 30 s
