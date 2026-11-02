"""Kalshi scores by REST polling (``GET /live_data/batch``), no network: parser, poller, hub, lookup, wiring.

The response layout is from the docs and the fixture is synthetic: these tests prove the code does what it says with
that layout, not that Kalshi answers that way (``LIVE_VERIFIED=false``). Display data only.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from mm_agent import eventview, eventview_source
from mm_agent.domain import VenueId
from mm_agent.live.hub import LiveHub
from mm_agent.live.kalshi_sports import (
    BATCH_MAX,
    SOURCE,
    KalshiGame,
    KalshiLiveDataFeed,
    parse_live_data,
)
from mm_agent.live.polymarket_sports_ws import parse_game
from mm_agent.live.service import build_hub
from tests.test_eventview import NOW, load
from tests.test_live_hub import Clock, Loader, drain

MILESTONE = "synthetic-milestone-1"
SECRET = "SECRET-TOKEN-123"


def payload() -> Any:
    return load("kalshi", "live_data_batch")


def game(**changes: Any) -> KalshiGame:
    base = {
        "milestone_id": MILESTONE,
        "home": "Boston",
        "away": "Los Angeles L",
        "home_score": 91,
        "away_score": 88,
        "period": "Q3",
        "live": True,
        "ended": False,
        "status": "live",
    }
    return KalshiGame(**{**base, **changes})


async def until(check: Callable[[], bool], timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not check():
        assert loop.time() < end, "condition not reached in time"
        await asyncio.sleep(0.005)


# ------------------------------------------------------------------------------------------------- parsing
def test_the_fixture_parses_into_one_game_with_whole_number_scores() -> None:
    assert parse_live_data(payload()) == [game()]


def test_scores_that_are_not_whole_numbers_are_not_scores() -> None:
    def parse(details: dict[str, Any]) -> KalshiGame:
        (g,) = parse_live_data({"live_datas": [{"milestone_id": "m1", "details": {"status": "live", **details}}]})
        return g

    assert (parse({"home_points": 12.5, "away_points": True}).home_score, parse({"away_points": True}).away_score) == (
        None,
        None,
    )
    assert parse({"home_points": "12", "away_points": " 7 "}).away_score == 7
    assert parse({"home_points": -3, "away_points": "x1"}).home_score is None
    assert parse({"home_points": None, "home_score": 4}).home_score == 4  # the next spelling is tried


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        [],
        "live",
        42,
        {"live_datas": "x"},
        {"live_datas": [None, 3, "a"]},
        {"live_datas": [{"details": {"status": "live"}}]},  # no milestone id
        {"live_datas": [{"milestone_id": 5, "details": {"status": "live"}}]},
        {"live_datas": [{"milestone_id": "a b", "details": {"status": "live"}}]},  # not an id
        {"live_datas": [{"milestone_id": "../x", "details": {"status": "live"}}]},
        {"live_datas": [{"milestone_id": "m1", "details": {}}]},  # nothing usable
        {"live_datas": [{"milestone_id": "m1", "details": "text"}]},
    ],
)
def test_unknown_layouts_and_unusable_rows_give_no_game(bad: Any) -> None:
    assert parse_live_data(bad) == []


def test_other_spellings_and_an_ended_game() -> None:
    (g,) = parse_live_data(
        {
            "live_data": {
                "id": "m9",
                "home_team": {"name": "Arsenal"},
                "away_team": "Chelsea",
                "home_score": "2",
                "away_score": 1,
                "quarter": 4,
                "status": "Final",
                "updated_at": "2026-11-02T01:02:03Z",
            }
        }
    )
    assert (g.home, g.away, g.home_score, g.away_score, g.period) == ("Arsenal", "Chelsea", 2, 1, "4")
    assert g.ended is True and g.live is False and g.source_ts == "2026-11-02T01:02:03Z"


# ------------------------------------------------------------------------------------------------- polling
class Fetcher:
    def __init__(self, reply: Any = None) -> None:
        self.calls: list[list[str]] = []
        self.reply = payload() if reply is None else reply
        self.fail: BaseException | None = None

    async def __call__(self, ids: list[str]) -> Any:
        self.calls.append(list(ids))
        if self.fail is not None:
            raise self.fail
        return self.reply


async def test_nothing_watched_means_no_request() -> None:
    fetch = Fetcher()
    feed = KalshiLiveDataFeed(fetch, on_game=lambda g: None, interval=0.01)
    assert await feed.poll_once() == 0
    feed.start()
    await asyncio.sleep(0.1)
    await feed.stop()
    assert fetch.calls == [] and feed.stats.connected is False


async def test_ids_go_out_in_batches_of_at_most_one_hundred() -> None:
    fetch = Fetcher(reply={"live_datas": []})
    feed = KalshiLiveDataFeed(fetch, on_game=lambda g: None)
    feed.watch([f"m{n:03d}" for n in range(250)])
    await feed.poll_once()
    assert [len(c) for c in fetch.calls] == [BATCH_MAX, BATCH_MAX, 50]
    assert sorted(i for c in fetch.calls for i in c) == feed.watched and feed.stats.subs == 250


async def test_only_games_that_were_asked_for_are_reported() -> None:
    seen: list[KalshiGame] = []
    reply = {"live_datas": [*payload()["live_datas"], {"milestone_id": "not-asked", "details": {"status": "live"}}]}
    feed = KalshiLiveDataFeed(Fetcher(reply), on_game=seen.append)
    feed.watch([MILESTONE])
    assert await feed.poll_once() == 1
    assert [g.milestone_id for g in seen] == [MILESTONE] and feed.stats.connected is True and feed.stats.total == 1


async def test_junk_ids_are_never_sent_and_the_watch_list_is_capped() -> None:
    fetch = Fetcher(reply={})
    feed = KalshiLiveDataFeed(fetch, on_game=lambda g: None)
    feed.watch(["ok-1", "bad id", "a/b", "", "x" * 200, "ok-2"])
    assert feed.watched == ["ok-1", "ok-2"]
    feed.unwatch(["ok-1"])
    assert feed.watched == ["ok-2"] and feed.stats.subs == 1


async def test_the_loop_polls_while_something_is_watched_and_stops_when_told() -> None:
    fetch = Fetcher()
    seen: list[KalshiGame] = []
    feed = KalshiLiveDataFeed(fetch, on_game=seen.append, interval=0.01)
    feed.start()
    feed.start()  # idempotent
    feed.watch([MILESTONE])
    await until(lambda: len(fetch.calls) >= 3)
    await feed.stop()
    n = len(fetch.calls)
    await asyncio.sleep(0.05)
    assert len(fetch.calls) == n and feed.stats.connected is False and seen


async def test_a_failing_venue_does_not_kill_the_loop_and_recovery_is_noticed() -> None:
    fetch = Fetcher()
    fetch.fail = RuntimeError("boom")
    feed = KalshiLiveDataFeed(fetch, on_game=lambda g: None, interval=0.01)
    feed.watch([MILESTONE])
    feed.start()
    await until(lambda: len(fetch.calls) >= 2)
    assert feed.stats.connected is False
    fetch.fail = None
    await until(lambda: feed.stats.connected)
    await feed.stop()


async def test_retry_after_is_honoured_not_hammered() -> None:
    class TooMany(RuntimeError):
        retry_after = 0.4

    fetch = Fetcher()
    fetch.fail = TooMany("429")
    feed = KalshiLiveDataFeed(fetch, on_game=lambda g: None, interval=0.01)
    feed.watch([MILESTONE])
    feed.start()
    await asyncio.sleep(0.2)
    await feed.stop()
    assert len(fetch.calls) == 1  # one try, then it waited for the venue


async def test_a_failure_logs_the_error_class_and_never_its_text(capsys: pytest.CaptureFixture[str]) -> None:
    fetch = Fetcher()
    fetch.fail = RuntimeError(f"upstream said {SECRET}")
    feed = KalshiLiveDataFeed(fetch, on_game=lambda g: None, interval=0.01)
    feed.watch([MILESTONE])
    feed.start()
    await until(lambda: len(fetch.calls) >= 1)
    await feed.stop()
    out = capsys.readouterr()
    logged = out.out + out.err  # the app's logger writes to the console, not through the logging module
    assert "kalshi_live_data_failed" in logged and "RuntimeError" in logged  # it is logged ...
    assert SECRET not in logged  # ... without the error's text


# -------------------------------------------------------------------------------------------------- the hub
class FakePoller:
    """What the hub needs from the poller: a name, stats, watch/unwatch, a lifecycle."""

    name = "kalshi_live_data"

    def __init__(self, clock: Clock) -> None:
        from mm_agent.live.common import FeedStats

        self.stats = FeedStats(clock)
        self.watched: list[list[str]] = []
        self.unwatched: list[list[str]] = []

    def watch(self, ids: Any) -> None:
        self.watched.append(sorted(ids))

    def unwatch(self, ids: Any) -> None:
        self.unwatched.append(sorted(ids))

    def start(self) -> None: ...

    async def stop(self) -> None: ...


def kalshi_view(milestones: list[str] | None = None) -> dict[str, Any]:
    raw = load("kalshi", "sports_event")
    return eventview.build_kalshi_view(raw, now=NOW, milestone_ids=[MILESTONE] if milestones is None else milestones)


def make(*, poller: bool = True) -> tuple[LiveHub, FakePoller | None, Clock]:
    clock = Clock()
    hub = LiveHub(load_view=Loader(kalshi_view()), clock=clock, wall=clock)
    fake = FakePoller(clock) if poller else None
    hub.attach(kalshi_sports=fake)
    return hub, fake, clock


async def follow(hub: LiveHub, who: str = "u") -> Any:
    sink = hub.connect(who)
    await hub.handle(sink, {"op": "subscribe", "topic": "event", "venue": "kalshi", "id": "KXNBAGAME-26NOV02LALBOS"})
    return sink


def flush(hub: LiveHub, clock: Clock) -> int:
    clock.t += 1
    return hub.flush()


async def test_the_view_carries_the_milestone_and_the_hub_follows_it_once() -> None:
    assert kalshi_view()["raw_extra"]["milestone_ids"] == [MILESTONE]
    assert "milestone_ids" not in eventview.build_kalshi_view(load("kalshi", "binary_event"), now=NOW)["raw_extra"]
    hub, fake, _ = make()
    assert fake is not None
    first, second = await follow(hub, "a"), await follow(hub, "b")
    assert fake.watched == [[MILESTONE]]
    hub.disconnect(first)
    assert fake.unwatched == []
    hub.disconnect(second)
    assert fake.unwatched == [[MILESTONE]] and hub._milestone_index == {}


async def test_the_first_score_gives_every_client_a_fresh_snapshot_with_the_game() -> None:
    hub, _, _ = make()
    sink = await follow(hub)
    (first,) = drain(sink)
    assert first["view"].get("game") is None
    hub.on_kalshi_game(game())
    (snap,) = drain(sink)
    assert snap["type"] == "snapshot" and snap["seq"] == first["seq"] + 1
    g = snap["view"]["game"]
    assert g["source"] == SOURCE == "kalshi_rest_polled"
    assert (g["home"]["name"], g["home"]["score"], g["away"]["name"], g["away"]["score"]) == (
        "Boston",
        91,
        "Los Angeles L",
        88,
    )
    assert g["period"] == "Q3" and g["live"] is True and snap["view"]["status"] == "live"


async def test_later_scores_are_small_patches_and_the_end_closes_the_game() -> None:
    hub, _, clock = make()
    sink = await follow(hub)
    hub.on_kalshi_game(game())
    drain(sink)

    hub.on_kalshi_game(game(home_score=94))
    assert flush(hub, clock) == 1
    (patch,) = drain(sink)
    assert patch["type"] == "patch" and patch["patch"]["game"]["home"] == {"score": 94}
    assert "away" not in patch["patch"]["game"] and patch["patch"]["game"]["source"] == SOURCE

    hub.on_kalshi_game(game(home_score=94, live=False, ended=True, status="final"))
    flush(hub, clock)
    (patch,) = drain(sink)
    assert patch["patch"]["status"] == "ended" and patch["patch"]["game"]["ended"] is True


async def test_a_score_for_a_milestone_nobody_follows_changes_nothing() -> None:
    hub, _, clock = make()
    sink = await follow(hub)
    drain(sink)
    hub.on_kalshi_game(game(milestone_id="someone-elses"))
    assert flush(hub, clock) == 0 and drain(sink) == []


async def test_the_polymarket_sports_feed_never_writes_into_a_kalshi_game() -> None:
    hub, _, clock = make()
    sink = await follow(hub)
    hub.on_kalshi_game(game())
    drain(sink)
    hub.on_game(
        parse_game({"gameId": 1, "homeTeam": "Boston", "awayTeam": "Los Angeles L", "score": "1-0", "live": True})
    )  # type: ignore[arg-type]
    assert flush(hub, clock) == 0 and drain(sink) == []
    state = hub._states[("kalshi", "KXNBAGAME-26NOV02LALBOS")]
    assert state.view["game"]["source"] == SOURCE and state.view["game"]["home"]["score"] == 91


async def test_an_event_without_milestones_or_a_hub_without_the_poller_is_unchanged() -> None:
    hub, fake, _ = make()
    assert fake is not None
    hub._load_view = Loader(kalshi_view([]))  # type: ignore[assignment]
    await follow(hub)
    assert fake.watched == []
    bare, _, _ = make(poller=False)
    sink = await follow(bare)  # milestone ids in the view, no poller: nothing to watch, nothing breaks
    assert drain(sink)[0]["type"] == "snapshot" and bare._milestone_index == {}


async def test_status_lists_the_poller_with_the_usual_numbers() -> None:
    hub, _, _ = make()
    feeds = hub.metrics()["feeds"]
    assert feeds["kalshi_live_data"].keys() >= {
        "connected",
        "reconnects",
        "msgs_per_s",
        "last_msg_age_s",
        "subs",
        "dropped",
    }


# ------------------------------------------------------------------------------- finding the milestone for an event
class KalshiRest:
    base_url = "https://kalshi.example.invalid/trade-api/v2"

    def __init__(self, *, category: str = "Sports", milestones: Any = None, fail: bool = False) -> None:
        self.category = category
        self.milestones = (
            {"milestones": [{"id": "m1"}, {"id": "bad id"}, {"id": "m2"}]} if milestones is None else milestones
        )
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((url.removeprefix(self.base_url), params))
        if url.endswith("/milestones"):
            if self.fail:
                raise RuntimeError(f"nope {SECRET}")
            return self.milestones
        event = copy.deepcopy(load("kalshi", "sports_event"))
        event["category"] = self.category
        return {"event": event}


@pytest.fixture(autouse=True)
def _fresh_milestone_cache() -> None:
    eventview_source._MILESTONES.clear()


async def test_a_sports_event_gets_its_milestones_from_the_documented_lookup() -> None:
    rest = KalshiRest()
    view = await eventview_source.kalshi_view(rest, ticker="KXNBAGAME-26NOV02LALBOS")
    assert view is not None and view["raw_extra"]["milestone_ids"] == ["m1", "m2"]
    _, params = next(c for c in rest.calls if c[0] == "/milestones")
    assert params == {"related_event_ticker": "KXNBAGAME-26NOV02LALBOS", "limit": 10}
    await eventview_source.kalshi_view(rest, ticker="KXNBAGAME-26NOV02LALBOS")  # a REST refresh: no second lookup
    assert [c[0] for c in rest.calls].count("/milestones") == 1


async def test_other_categories_are_never_looked_up() -> None:
    rest = KalshiRest(category="Economics")
    view = await eventview_source.kalshi_view(rest, ticker="KXNBAGAME-26NOV02LALBOS")
    assert view is not None and "milestone_ids" not in view["raw_extra"]
    assert "/milestones" not in [c[0] for c in rest.calls]


@pytest.mark.parametrize("milestones", [None, {}, {"milestones": "x"}, []])
async def test_a_failed_or_odd_lookup_still_gives_a_view(milestones: Any) -> None:
    rest = KalshiRest(fail=milestones is None, milestones=milestones if milestones is not None else {})
    view = await eventview_source.kalshi_view(rest, ticker="KXNBAGAME-26NOV02LALBOS")
    assert view is not None and view["layout"] == "multi_candidates" and "milestone_ids" not in view["raw_extra"]


# ------------------------------------------------------------------------------------------------- the wiring
class FakeRegistry:
    def __init__(self, adapter: Any) -> None:
        self.adapters = {VenueId.KALSHI: adapter}


class WiredKalshi(KalshiRest):
    def ws_headers(self) -> dict[str, str]:
        raise RuntimeError("no key")

    def ws_url(self) -> str:
        return "wss://kalshi.example.invalid/ws"


async def test_build_hub_attaches_the_poller_and_it_sends_the_documented_request() -> None:
    rest = WiredKalshi()
    hub = build_hub(SimpleNamespace(adapters=FakeRegistry(rest).adapters), run_feeds=True)
    assert isinstance(hub.kalshi_sports, KalshiLiveDataFeed) and hub.kalshi_sports.interval == 2.5
    await hub.kalshi_sports.fetch(["a", "b"])
    assert rest.calls[-1] == ("/live_data/batch", {"milestone_ids": "a,b"})
    assert "kalshi_live_data" in hub.metrics()["feeds"]
    assert build_hub(SimpleNamespace(adapters=FakeRegistry(rest).adapters), run_feeds=False).kalshi_sports is None
