"""Kalshi and Limitless feeds wired through the hub (no network, stub feeds).

The feed protocols themselves are in ``test_live_venue_feeds.py``. Here: the ``(venue, asset)`` index keeps the three
venues apart, staleness is decided per venue, settlement reaches the right rows, and a venue without its feed still
works over REST and says so. Display data only; nothing here can place an order.
"""

from __future__ import annotations

import copy
from typing import Any

from mm_agent import eventview
from mm_agent.live.hub import LiveHub
from tests.test_eventview import NOW, load, pm
from tests.test_live_hub import Clock, StubFeed, drain, q

TICKER_A = "KXSYN-26NOV01-T3.0"
TICKER_B = "KXSYN-26NOV01-T3.5"
SLUG = "synthetic-group-a"


class VenueFeed(StubFeed):
    """A stub that answers to the name the real feed has (``kalshi_ws``, ``limitless_ws``)."""

    def __init__(self, name: str, clock: Clock, connected: bool = True) -> None:
        super().__init__(clock, connected)
        self.name = name


class VenueLoader:
    """Returns one view per venue, like the real loader does for the venue named in the call."""

    def __init__(self, views: dict[str, dict[str, Any]]) -> None:
        self.views = views
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, venue: str, ident: str) -> dict[str, Any] | None:
        self.calls.append((venue, ident))
        view = self.views.get(venue)
        return copy.deepcopy(view) if view is not None else None


def kalshi_view() -> dict[str, Any]:
    raw = load("kalshi", "range_event")
    for market in raw["markets"]:
        market["status"] = "active"
    return eventview.build_kalshi_view(raw, now=NOW)


def limitless_view() -> dict[str, Any]:
    return eventview.build_limitless_view(load("limitless", "group"), now=NOW)


def polymarket_view_sharing_a_kalshi_ticker() -> dict[str, Any]:
    """A Polymarket event whose YES token happens to be spelled like a Kalshi ticker."""

    view = pm("candidates")
    view["sections"][0]["rows"][0]["token_ids"] = [TICKER_A, "pm-no-token"]
    return view


class World:
    def __init__(self, *, kalshi: bool = True, limitless: bool = True, **kw: Any) -> None:
        self.clock = Clock()
        self.loader = VenueLoader(
            {
                "polymarket": polymarket_view_sharing_a_kalshi_ticker(),
                "kalshi": kalshi_view(),
                "limitless": limitless_view(),
            }
        )
        self.hub = LiveHub(load_view=self.loader, clock=self.clock, wall=self.clock, **kw)
        self.market = StubFeed(self.clock)
        self.kalshi = VenueFeed("kalshi_ws", self.clock) if kalshi else None
        self.limitless = VenueFeed("limitless_ws", self.clock) if limitless else None
        self.hub.attach(market=self.market, kalshi=self.kalshi, limitless=self.limitless)

    async def follow(self, venue: str, ident: str) -> Any:
        sink = self.hub.connect(f"user-{venue}")
        await self.hub.handle(sink, {"op": "subscribe", "topic": "event", "venue": venue, "id": ident})
        return sink

    def flush(self) -> int:
        self.clock.t += 1
        return self.hub.flush()


# --------------------------------------------------------------------------------------- the (venue, asset) index
async def test_each_feed_is_told_only_about_its_own_venues_assets() -> None:
    w = World()
    await w.follow("polymarket", "cand")
    await w.follow("kalshi", "KXSYN-26NOV01")
    await w.follow("limitless", "synthetic-group")
    assert w.kalshi is not None and w.limitless is not None
    assert w.kalshi.watched == [[TICKER_A, TICKER_B]]
    assert w.limitless.watched == [[SLUG]]
    assert TICKER_A in w.market.watched[0] and TICKER_B not in w.market.watched[0] and SLUG not in w.market.watched[0]


async def test_the_same_asset_text_on_two_venues_is_two_different_assets() -> None:
    w = World()
    pm_sink = await w.follow("polymarket", "cand")
    k_sink = await w.follow("kalshi", "KXSYN-26NOV01")
    drain(pm_sink)
    drain(k_sink)

    w.hub.on_quote(q(TICKER_A, "0.70", "0.72"), "kalshi")
    assert w.flush() == 1
    assert drain(pm_sink) == []  # the Polymarket token with the same spelling did not move
    (patch,) = drain(k_sink)
    row = patch["patch"]["rows"][TICKER_A]
    assert patch["type"] == "patch" and patch["source"] == "ws"
    assert row["chance"] == "0.71" and row["yes"]["ask"] == "0.72" and row["yes"]["bid"] == "0.7"

    w.hub.on_quote(q(TICKER_A, "0.10", "0.12"))  # default venue: polymarket
    assert w.flush() == 1
    assert drain(k_sink) == []
    (patch,) = drain(pm_sink)
    assert patch["patch"]["rows"]["c1"]["chance"] == "0.11"


async def test_a_quote_for_an_asset_nobody_follows_changes_nothing() -> None:
    w = World()
    sink = await w.follow("kalshi", "KXSYN-26NOV01")
    drain(sink)
    w.hub.on_quote(q("KXOTHER-26NOV01-T1.0", "0.40", "0.42"), "kalshi")
    w.hub.on_quote(q(TICKER_A, "0.40", "0.42"), "limitless")  # right text, wrong venue
    assert w.flush() == 0 and drain(sink) == []


async def test_upstream_is_released_per_venue_when_the_last_client_leaves() -> None:
    w = World()
    assert w.kalshi is not None and w.limitless is not None
    pm_sink = await w.follow("polymarket", "cand")
    k_sink = await w.follow("kalshi", "KXSYN-26NOV01")
    l_sink = await w.follow("limitless", "synthetic-group")
    w.hub.disconnect(k_sink)
    assert w.kalshi.unwatched == [[TICKER_A, TICKER_B]]
    assert w.market.unwatched == [] and w.limitless.unwatched == []
    assert ("polymarket", TICKER_A) in w.hub._asset_index and ("kalshi", TICKER_A) not in w.hub._asset_index
    w.hub.disconnect(l_sink)
    w.hub.disconnect(pm_sink)
    assert w.limitless.unwatched == [[SLUG]] and w.hub._asset_index == {}


async def test_two_clients_on_one_kalshi_event_share_one_upstream_subscription() -> None:
    w = World()
    assert w.kalshi is not None
    first = await w.follow("kalshi", "KXSYN-26NOV01")
    second = await w.follow("kalshi", "KXSYN-26NOV01")
    assert w.kalshi.watched == [[TICKER_A, TICKER_B]] and w.loader.calls.count(("kalshi", "KXSYN-26NOV01")) == 1
    w.hub.disconnect(first)
    assert w.kalshi.unwatched == []
    w.hub.disconnect(second)
    assert w.kalshi.unwatched == [[TICKER_A, TICKER_B]]


# ------------------------------------------------------------------------------------------ staleness per venue
async def test_a_kalshi_feed_that_drops_makes_only_kalshi_events_stale() -> None:
    w = World(rest_interval=2)
    pm_sink = await w.follow("polymarket", "cand")
    k_sink = await w.follow("kalshi", "KXSYN-26NOV01")
    l_sink = await w.follow("limitless", "synthetic-group")
    for sink in (pm_sink, k_sink, l_sink):
        drain(sink)
    w.loader.calls.clear()

    w.hub.on_feed_state("kalshi_ws", False)
    assert w.flush() >= 1
    status, patch = drain(k_sink)  # the stale flag at once, then the freshness change in the next patch
    assert status["type"] == "status" and status["stale"] is True and status["source"] == "rest"
    assert patch["type"] == "patch" and patch["patch"]["freshness"]["stale"] is True
    assert drain(pm_sink) == [] and drain(l_sink) == []

    w.clock.t += 3
    await w.hub.housekeeping()
    assert w.loader.calls == [("kalshi", "KXSYN-26NOV01")]  # REST fallback for the stale venue only

    w.hub.on_feed_state("kalshi_ws", True)
    w.hub.on_quote(q(TICKER_A, "0.50", "0.52"), "kalshi")  # first book after the reconnect proves it is live
    w.flush()
    frames = drain(k_sink)
    assert frames[-1]["type"] == "snapshot" and frames[-1]["view"]["freshness"]["stale"] is False
    assert frames[-1]["source"] == "ws"


async def test_a_silent_limitless_feed_goes_stale_while_the_others_stay_fresh() -> None:
    w = World(stale_after=45)
    assert w.limitless is not None and w.kalshi is not None
    pm_sink = await w.follow("polymarket", "cand")
    k_sink = await w.follow("kalshi", "KXSYN-26NOV01")
    l_sink = await w.follow("limitless", "synthetic-group")
    for sink in (pm_sink, k_sink, l_sink):
        drain(sink)
    for feed in (w.market, w.kalshi, w.limitless):
        feed.stats.message()
    w.clock.t += 30
    w.market.stats.message()
    w.kalshi.stats.message()
    await w.hub.housekeeping()
    assert drain(l_sink) == []  # 30 s of silence is not yet stale
    w.clock.t += 30
    w.market.stats.message()
    w.kalshi.stats.message()
    await w.hub.housekeeping()
    (status,) = drain(l_sink)
    assert status["type"] == "status" and status["stale"] is True
    assert drain(pm_sink) == [] and drain(k_sink) == []


async def test_a_venue_with_no_feed_at_all_is_rest_polled_and_says_so() -> None:
    w = World(kalshi=False, poll_interval=5)
    w.hub.attach(market=w.market, limitless=w.limitless, unavailable={"kalshi_ws": "no Kalshi API key configured"})
    sink = await w.follow("kalshi", "KXSYN-26NOV01")
    (snap,) = drain(sink)
    assert snap["source"] == "rest" and snap["view"]["freshness"]["source"] == "rest"
    assert snap["view"]["freshness"]["stale"] is False  # no feed was promised, so nothing is stale
    feeds = w.hub.metrics()["feeds"]
    assert feeds["kalshi_ws"] == {"connected": False, "available": False, "reason": "no Kalshi API key configured"}

    changed = kalshi_view()
    changed["sections"][0]["rows"][0]["chance"] = "0.20"
    w.loader.views["kalshi"] = changed
    w.clock.t += 6
    await w.hub.housekeeping()
    w.flush()
    (patch,) = drain(sink)
    assert patch["patch"]["rows"][TICKER_A]["chance"] == "0.20"


# ------------------------------------------------------------------------------------------------ on_settled
async def test_kalshi_settlement_marks_the_row_then_the_event() -> None:
    w = World()
    sink = await w.follow("kalshi", "KXSYN-26NOV01")
    drain(sink)

    w.hub.on_settled("kalshi", TICKER_A, True)
    w.flush()
    (patch,) = drain(sink)
    assert patch["patch"]["rows"][TICKER_A] == {"status": "resolved", "chance": "1"}
    assert "status" not in patch["patch"]  # the other row is still open, so the event is still live

    w.hub.on_settled("kalshi", TICKER_B, None)  # settled, result unknown: no invented 0 or 1
    w.flush()
    (patch,) = drain(sink)
    assert patch["patch"]["rows"][TICKER_B] == {"status": "resolved"}
    assert patch["patch"]["status"] == "resolved"


async def test_limitless_settlement_by_slug_and_a_no_result() -> None:
    w = World()
    sink = await w.follow("limitless", "synthetic-group")
    drain(sink)
    w.hub.on_settled("limitless", SLUG, False)
    w.flush()
    (patch,) = drain(sink)
    assert patch["patch"]["rows"][SLUG] == {"status": "resolved", "chance": "0"}
    assert patch["patch"]["status"] == "resolved"


async def test_settlement_only_reaches_the_venue_that_reported_it() -> None:
    w = World()
    pm_sink = await w.follow("polymarket", "cand")
    k_sink = await w.follow("kalshi", "KXSYN-26NOV01")
    drain(pm_sink)
    drain(k_sink)
    w.hub.on_settled("limitless", TICKER_A, True)  # right text, wrong venue
    w.hub.on_settled("kalshi", "KXNOBODY-26NOV01-T9.9", True)  # nobody follows it
    assert w.flush() == 0 and drain(pm_sink) == [] and drain(k_sink) == []
    w.hub.on_settled("kalshi", TICKER_A, True)
    w.flush()
    assert len(drain(k_sink)) == 1 and drain(pm_sink) == []
