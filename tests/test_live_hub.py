"""The live hub (no network): snapshots, patches, coalescing, ref counts, stale fallback, slow clients, /v1/stream."""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mm_agent.control_api import create_app
from mm_agent.live.common import FeedStats, Quote
from mm_agent.live.hub import ClientSink, LiveHub
from mm_agent.live.polymarket_sports_ws import parse_game
from tests.conftest import Harness
from tests.test_eventview import pm, soccer

FEED = "polymarket_market"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class StubFeed:
    """Records what the hub asks the upstream feed to follow."""

    name = FEED

    def __init__(self, clock: Clock, connected: bool = True) -> None:
        self.stats = FeedStats(clock)
        self.stats.connected = connected
        self.watched: list[list[str]] = []
        self.unwatched: list[list[str]] = []

    def watch(self, assets: Any) -> None:
        self.watched.append(sorted(assets))

    def unwatch(self, assets: Any) -> None:
        self.unwatched.append(sorted(assets))

    def start(self) -> None: ...

    async def stop(self) -> None: ...


class Loader:
    def __init__(self, view: dict[str, Any]) -> None:
        self.view = view
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, venue: str, ident: str) -> dict[str, Any] | None:
        self.calls.append((venue, ident))
        return None if ident == "missing" else copy.deepcopy(self.view)


def make_hub(
    view: dict[str, Any] | None = None, *, feed: bool = True, history: Any = None, **kw: Any
) -> tuple[LiveHub, Loader, Clock, StubFeed | None]:
    clock = Clock()
    loader = Loader(view if view is not None else pm("candidates"))
    hub = LiveHub(load_view=loader, history=history, clock=clock, wall=clock, **kw)
    stub = StubFeed(clock) if feed else None
    hub.attach(market=stub)
    return hub, loader, clock, stub


def drain(sink: ClientSink) -> list[dict[str, Any]]:
    out = []
    while sink.pending():
        out.append(sink._frames.popleft())
    return out


async def sub(hub: LiveHub, sink: ClientSink, ident: str = "cand", **extra: Any) -> None:
    await hub.handle(sink, {"op": "subscribe", "topic": "event", "venue": "polymarket", "id": ident, **extra})


def q(asset: str, bid: str | None, ask: str | None, last: str | None = None) -> Quote:
    return Quote(
        asset,
        Decimal(bid) if bid else None,
        Decimal(ask) if ask else None,
        Decimal(last) if last else None,
        "1700000000123",
    )


# ------------------------------------------------------------------------------------------ snapshot, patch
async def test_snapshot_then_patches_with_seq_source_and_timestamps() -> None:
    hub, _, clock, _ = make_hub()
    sink = hub.connect("u")
    await sub(hub, sink)
    (snap,) = drain(sink)
    assert snap["type"] == "snapshot" and snap["seq"] == 0 and snap["venue"] == "polymarket"
    assert snap["view"]["layout"] == "multi_candidates" and snap["server_ts"] and snap["source"] == "ws"
    assert snap["view"]["freshness"] == {
        "price_ts": snap["view"]["freshness"]["price_ts"],
        "source": "ws",
        "stale": False,
    }
    hub.on_quote(q("c11", "0.50", "0.54"))
    clock.t += 1
    assert hub.flush() == 1
    (patch,) = drain(sink)
    assert (patch["type"], patch["seq"], patch["source"], patch["source_ts"]) == ("patch", 1, "ws", "1700000000123")
    row = patch["patch"]["rows"]["c1"]
    assert row["chance"] == "0.52"  # midpoint, not the ask
    assert row["yes"] == {"ask": "0.54", "bid": "0.5"} and row["no"] == {"ask": "0.5", "bid": "0.46"}
    assert patch["patch"]["freshness"]["stale"] is False
    # a snapshot taken afterwards already contains the change
    await hub.handle(sink, {"op": "snapshot", "venue": "polymarket", "id": "cand"})
    again = drain(sink)[0]["view"]["sections"][0]["rows"][0]
    assert again["chance"] == "0.52" and again["yes"]["ask"] == "0.54"


async def test_coalescing_latest_wins_and_at_most_four_patches_per_second() -> None:
    hub, _, clock, _ = make_hub()
    sink = hub.connect()
    await sub(hub, sink)
    drain(sink)
    for i in range(10):
        hub.on_quote(q("c11", f"0.{40 + i}", f"0.{50 + i}"))
    assert hub.flush() == 1
    patches = drain(sink)
    assert len(patches) == 1 and patches[0]["patch"]["rows"]["c1"]["yes"]["ask"] == "0.59"
    hub.on_quote(q("c11", "0.60", "0.62"))
    assert hub.flush() == 0  # same instant: held back, not lost
    clock.t += 0.1
    assert hub.flush() == 0
    clock.t += 0.2
    assert hub.flush() == 1
    assert drain(sink)[0]["seq"] == 2
    assert hub.flush() == 0  # nothing pending, nothing sent


async def test_unchanged_quote_sends_nothing() -> None:
    hub, _, clock, _ = make_hub()
    sink = hub.connect()
    await sub(hub, sink)
    hub.on_quote(q("c11", "0.50", "0.54"))
    clock.t += 1
    hub.flush()
    drain(sink)
    hub.on_quote(q("c11", "0.50", "0.54"))
    clock.t += 1
    assert hub.flush() == 0


async def test_no_asks_means_no_fake_prices() -> None:
    hub, _, clock, _ = make_hub()
    sink = hub.connect()
    await sub(hub, sink)
    drain(sink)
    hub.on_quote(q("c11", "0.50", None, last="0.51"))  # an empty ask side
    clock.t += 1
    hub.flush()
    row = drain(sink)[0]["patch"]["rows"]["c1"]
    assert row["chance"] == "0.51" and row["yes"]["ask"] is None and row["no"]["bid"] is None


# ------------------------------------------------------------------------------------------ ref counts
async def test_upstream_is_ref_counted_and_aliases_share_one_state() -> None:
    hub, loader, _, feed = make_hub()
    assert feed is not None
    a, b = hub.connect(), hub.connect()
    await sub(hub, a, "cand")
    await sub(hub, b, "cand-slug")  # same event under another name: its canonical id is the same
    await sub(hub, b, "cand")
    assert len(feed.watched) == 1 and feed.watched[0] == ["c11", "c21", "c31"]
    assert hub.metrics()["events"] == 1
    await hub.handle(a, {"op": "unsubscribe", "venue": "polymarket", "id": "cand"})
    assert feed.unwatched == []  # b still watches
    hub.disconnect(b)
    assert feed.unwatched == [["c11", "c21", "c31"]]
    assert hub.metrics()["events"] == 0 and hub.metrics()["clients"] == 1
    assert [c[1] for c in loader.calls].count("cand") == 1


async def test_two_events_sharing_a_token_unwatch_it_only_after_the_last() -> None:
    hub, loader, _, feed = make_hub()
    assert feed is not None
    a, b = hub.connect(), hub.connect()
    await sub(hub, a, "one")
    second = copy.deepcopy(loader.view)
    second["event_id"] = "second"
    second["sections"][0]["rows"] = second["sections"][0]["rows"][:1]  # only the market whose YES token is c11
    loader.view = second
    await sub(hub, b, "two")
    assert feed.watched == [["c11", "c21", "c31"]]  # c11 is already followed: not asked for twice
    hub.disconnect(a)
    assert feed.unwatched == [["c21", "c31"]]  # c11 is still owned by the second event
    hub.disconnect(b)
    assert feed.unwatched[-1] == ["c11"]


# ------------------------------------------------------------------------------------------ slow clients
async def test_slow_client_drops_old_patches_never_the_snapshot() -> None:
    hub, _, clock, _ = make_hub(max_patches=3)
    slow = hub.connect()
    await sub(hub, slow)
    for i in range(10):
        hub.on_quote(q("c11", f"0.{10 + i}", f"0.{20 + i}"))
        clock.t += 1
        hub.flush()
    frames = drain(slow)
    assert frames[0]["type"] == "snapshot"
    patches = [f for f in frames if f["type"] == "patch"]
    assert len(patches) == 3 and slow.dropped == 7
    assert patches[0]["seq"] == 8  # a gap after seq 0: the client asks for a snapshot
    await hub.handle(slow, {"op": "snapshot", "venue": "polymarket", "id": "cand"})
    assert drain(slow)[0]["view"]["sections"][0]["rows"][0]["yes"]["ask"] == "0.29"
    assert hub.metrics()["dropped"] == 7


async def test_a_snapshot_supersedes_queued_patches_of_that_event() -> None:
    hub, _, clock, _ = make_hub()
    sink = hub.connect()
    await sub(hub, sink)
    hub.on_quote(q("c11", "0.30", "0.40"))
    clock.t += 1
    hub.flush()
    await hub.handle(sink, {"op": "snapshot", "venue": "polymarket", "id": "cand"})
    assert [f["type"] for f in drain(sink)] == ["snapshot", "snapshot"]


async def test_idle_client_that_stopped_reading_is_evicted() -> None:
    hub, _, clock, feed = make_hub(client_idle_seconds=30)
    assert feed is not None
    sink = hub.connect()
    await sub(hub, sink)
    clock.t += 31
    await hub.housekeeping()
    assert sink.closed and hub.metrics()["clients"] == 0 and feed.unwatched


# ------------------------------------------------------------------------------------------ limits and errors
async def test_bad_requests_and_limits_answer_with_error_frames() -> None:
    hub, _, _, _ = make_hub(max_subs_per_client=1)
    sink = hub.connect()
    await hub.handle(sink, {"op": "explode"})
    await hub.handle(sink, {"op": "subscribe", "venue": "nowhere", "id": "x"})
    await hub.handle(sink, {"op": "subscribe", "venue": "polymarket", "id": "bad id!"})
    await hub.handle(sink, {"op": "subscribe", "venue": "polymarket", "id": "missing"})
    await hub.handle(sink, {"op": "snapshot", "venue": "polymarket", "id": "cand"})
    await sub(hub, sink, "cand")
    await sub(hub, sink, "other")
    codes = [f.get("code") for f in drain(sink)]
    assert codes == ["bad_op", "bad_request", "bad_request", "not_found", "not_subscribed", None, "limit"]


async def test_event_cap_and_loader_failure_are_reported() -> None:
    hub, loader, _, _ = make_hub(max_events=1)
    a, b = hub.connect(), hub.connect()
    await sub(hub, a, "cand")
    other = copy.deepcopy(loader.view)
    other["event_id"] = "second"
    loader.view = other
    await sub(hub, b, "another")
    assert drain(b)[0]["code"] == "limit"

    async def boom(venue: str, ident: str) -> None:
        raise RuntimeError("secret detail")

    hub2 = LiveHub(load_view=boom)
    c = hub2.connect()
    await sub(hub2, c)
    err = drain(c)[0]
    assert err["code"] == "load_failed" and "secret" not in err["detail"]


# ------------------------------------------------------------------------------------------ stale, fallback, recovery
async def test_feed_down_marks_stale_polls_rest_and_recovery_sends_a_fresh_snapshot() -> None:
    hub, loader, clock, feed = make_hub()
    assert feed is not None
    sink = hub.connect()
    await sub(hub, sink)
    drain(sink)
    hub.on_feed_state(FEED, False)
    feed.stats.connected = False
    (status,) = drain(sink)
    assert (status["type"], status["stale"], status["source"]) == ("status", True, "rest")
    # REST fallback: the venue now says something else; polled no faster than every 2 s
    changed = copy.deepcopy(loader.view)
    changed["sections"][0]["rows"][0].update(chance="0.77")
    loader.view = changed
    calls = len(loader.calls)
    clock.t += 1
    await hub.housekeeping()
    assert len(loader.calls) == calls  # too soon
    clock.t += 1.5
    await hub.housekeeping()
    assert len(loader.calls) == calls + 1
    hub.flush()
    patch = next(f for f in drain(sink) if f["type"] == "patch")
    assert patch["source"] == "rest" and patch["patch"]["rows"]["c1"]["chance"] == "0.77"
    assert patch["patch"]["freshness"] == {
        "price_ts": patch["patch"]["freshness"]["price_ts"],
        "source": "rest",
        "stale": True,
    }
    # the feed comes back: first book proves it, everyone gets stale:false and a fresh snapshot
    feed.stats.connected = True
    hub.on_feed_state(FEED, True)
    hub.on_quote(q("c11", "0.60", "0.64"))
    frames = drain(sink)
    assert [f["type"] for f in frames] == ["status", "snapshot"]
    assert (
        frames[0]["stale"] is False and frames[1]["source"] == "ws" and frames[1]["view"]["freshness"]["stale"] is False
    )
    calls = len(loader.calls)
    clock.t += 10
    await hub.housekeeping()
    assert len(loader.calls) == calls  # no more polling once the feed is back


async def test_a_silent_feed_goes_stale_even_while_connected() -> None:
    hub, _, clock, feed = make_hub(stale_after=45)
    assert feed is not None
    sink = hub.connect()
    await sub(hub, sink)
    drain(sink)
    feed.stats.message()
    clock.t += 20
    await hub.housekeeping()
    assert drain(sink) == []
    clock.t += 40
    await hub.housekeeping()
    assert drain(sink)[0]["stale"] is True


async def test_a_venue_without_a_feed_is_polled_by_rest_and_says_so() -> None:
    hub, loader, clock, _ = make_hub(feed=False, poll_interval=5)
    sink = hub.connect()
    await sub(hub, sink)
    assert drain(sink)[0]["source"] == "rest"
    changed = copy.deepcopy(loader.view)
    changed["sections"][0]["rows"][1].update(chance="0.11")
    loader.view = changed
    clock.t += 4
    await hub.housekeeping()
    hub.flush()
    assert drain(sink) == []
    clock.t += 2
    await hub.housekeeping()
    hub.flush()
    assert drain(sink)[0]["patch"]["rows"]["c2"]["chance"] == "0.11"


async def test_rest_polling_is_budgeted_per_tick() -> None:
    hub, loader, clock, _ = make_hub(feed=False, poll_interval=2, rest_per_tick=2)
    sink = hub.connect()
    for n in range(5):
        view = copy.deepcopy(loader.view)
        view["event_id"] = f"e{n}"
        loader.view = view
        await sub(hub, sink, f"e{n}")
    calls = len(loader.calls)
    clock.t += 3
    await hub.housekeeping()
    assert len(loader.calls) == calls + 2


# ------------------------------------------------------------------------------------------ resolution, game
async def test_market_resolved_marks_the_row_and_the_event() -> None:
    hub, _, clock, _ = make_hub()
    sink = hub.connect()
    await sub(hub, sink)
    drain(sink)
    hub.on_resolved(["c11", "c12"], "c11")
    clock.t += 1
    hub.flush()
    patch = drain(sink)[0]["patch"]
    assert patch["rows"]["c1"] == {"status": "resolved", "chance": "1"}


async def test_game_score_follows_the_names_the_sports_feed_sends() -> None:
    hub, _, clock, _ = make_hub(soccer())
    sink = hub.connect()
    await sub(hub, sink, "soccer")
    game = drain(sink)[0]["view"]["game"]
    assert (game["home"]["name"], game["away"]["name"]) == ("Arsenal", "Chelsea")
    # the feed says Chelsea is HOME and leads 3-0: the score must land on the right team
    msg = {"gameId": 9001, "homeTeam": "Chelsea", "awayTeam": "Arsenal", "score": "3-0", "period": "HT", "live": True}
    hub.on_game(parse_game(msg))  # type: ignore[arg-type]
    clock.t += 1
    hub.flush()
    patch = drain(sink)[0]["patch"]["game"]
    assert patch["away"]["score"] == 3 and patch["home"]["score"] == 0  # view home = Arsenal
    assert patch["period"] == "Half-time" and patch["source"] == "polymarket_sports" and patch["status_text"] == ""
    await hub.handle(sink, {"op": "snapshot", "venue": "polymarket", "id": "soccer"})
    view_game = drain(sink)[0]["view"]["game"]
    assert (view_game["home"]["score"], view_game["away"]["score"]) == (0, 3)


async def test_game_already_known_is_applied_before_the_first_snapshot() -> None:
    class Sports:
        stats = FeedStats()

        def find(self, game_id: Any, home: str, away: str) -> Any:
            return parse_game({"gameId": game_id, "homeTeam": home, "awayTeam": away, "score": "1-1", "live": True})

    hub, _, _, _ = make_hub(soccer())
    hub.sports = Sports()
    sink = hub.connect()
    await sub(hub, sink, "soccer")
    game = drain(sink)[0]["view"]["game"]
    assert (game["home"]["score"], game["away"]["score"], game["live"]) == (1, 1, True)


# ------------------------------------------------------------------------------------------ chart
async def test_chart_history_then_live_tail() -> None:
    async def history(tokens: list[str], rng: str) -> dict[str, list[list[Any]]]:
        return {t: [[100, "0.30"], [200, "0.35"]] for t in tokens}

    hub, _, clock, _ = make_hub(history=history)
    sink = hub.connect()
    await sub(hub, sink, range="1W")
    chart = drain(sink)[0]["view"]["chart"]
    assert chart["history"] == "ok" and chart["range"] == "1W"
    assert chart["series"][0]["points"] == [[100, "0.30"], [200, "0.35"]]
    hub.on_quote(q("c11", "0.50", "0.54"))
    clock.t += 1
    hub.flush()
    tail = drain(sink)[0]["patch"]["chart"]["c1"]
    assert tail == [[1000, "0.52"]]  # stamped when the tick arrived
    await hub.handle(sink, {"op": "snapshot", "venue": "polymarket", "id": "cand", "range": "1W"})
    assert drain(sink)[0]["view"]["chart"]["series"][0]["points"][-1] == [1000, "0.52"]


async def test_missing_history_is_stated_not_faked() -> None:
    hub, _, _, _ = make_hub()  # no history function
    sink = hub.connect()
    await sub(hub, sink)
    chart = drain(sink)[0]["view"]["chart"]
    assert chart["history"] == "unavailable" and all(s["points"] == [] for s in chart["series"])


# ------------------------------------------------------------------------------------------ /v1/stream
def stream_client(harness: Harness, hub: LiveHub | None = None) -> TestClient:
    app = create_app(
        harness.settings, harness.agent, harness.scanner, harness.registry, harness.chat, harness.bus, hub=hub
    )
    return TestClient(app)


def test_stream_rejects_an_unauthenticated_client(harness: Harness) -> None:
    client = stream_client(harness)
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/v1/stream"):
        pass
    assert exc.value.code == 4401


def test_stream_serves_snapshot_patch_and_errors_to_a_signed_in_client(harness: Harness) -> None:
    hub, _, clock, _ = make_hub()
    with stream_client(harness, hub) as client:
        assert (
            client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"}).status_code == 200
        )
        with client.websocket_connect("/v1/stream") as ws:
            assert ws.receive_json()["type"] == "hello"
            ws.send_text("not json")
            assert ws.receive_json()["code"] == "bad_frame"
            ws.send_json({"op": "subscribe", "topic": "event", "venue": "polymarket", "id": "cand"})
            snap = ws.receive_json()
            assert snap["type"] == "snapshot" and snap["view"]["layout"] == "multi_candidates"
            assert client.portal is not None
            client.portal.call(hub.on_quote, q("c11", "0.50", "0.54"))
            clock.t += 1
            client.portal.call(hub.flush)
            patch = ws.receive_json()
            assert patch["type"] == "patch" and patch["seq"] == 1 and patch["patch"]["rows"]["c1"]["chance"] == "0.52"
            ws.send_json({"op": "subscribe", "venue": "polymarket", "id": "missing"})
            assert ws.receive_json()["code"] == "not_found"
        assert hub.metrics()["clients"] == 0  # the disconnect released everything


def test_stream_is_closed_when_the_feature_flag_is_off(harness: Harness) -> None:
    off = harness.settings.model_copy(update={"live_views_enabled": False})
    app = create_app(off, harness.agent, harness.scanner, harness.registry, harness.chat, harness.bus)
    client = TestClient(app)
    client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/v1/stream"):
        pass
    assert exc.value.code == 4404
    assert "live" not in client.get("/status").json()


def test_status_reports_per_feed_metrics(harness: Harness) -> None:
    hub, _, _, _ = make_hub()
    client = stream_client(harness, hub)
    client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
    live = client.get("/status").json()["live"]
    assert live["feeds"][FEED].keys() >= {"connected", "reconnects", "msgs_per_s", "last_msg_age_s", "subs", "dropped"}
    assert live["events"] == 0 and live["clients"] == 0
