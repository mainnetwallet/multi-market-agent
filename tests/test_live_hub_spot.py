"""The Up/Down spot price and the name of its source (no network, fake spot feeds).

PolyBolt is not wired in the app (``WORKLOG.md``, P2a): ``CryptoPrices`` is built without it, so every spot the hub
shows must say ``coinbase``. The ``polybolt`` label is only ever produced when a PolyBolt price really exists.
"""

from __future__ import annotations

from typing import Any

from mm_agent.live.hub import LiveHub
from mm_agent.live.polybolt import CryptoPrices
from mm_agent.live.service import build_hub
from tests.test_eventview import pm
from tests.test_live_hub import Clock, Loader, StubFeed, drain


class FakeSpot:
    def __init__(self, price: str = "77696.35") -> None:
        self.price = price
        self.calls: list[str] = []

    async def snapshot(self, symbol: str) -> dict[str, Any]:
        self.calls.append(symbol)
        return {"symbol": symbol, "price": self.price, "as_of": "2026-11-01T12:00:00+00:00"}


class FakeBolt:
    def __init__(self, price: str | None) -> None:
        self._price = price
        self.watched: list[str] = []

    def watch(self, symbol: str) -> None:
        self.watched.append(symbol)

    def price(self, symbol: str) -> str | None:
        return self._price


def updown_hub(crypto: CryptoPrices) -> tuple[LiveHub, Clock]:
    clock = Clock()
    hub = LiveHub(load_view=Loader(pm("crypto_updown_5m")), clock=clock, wall=clock)
    hub.attach(market=StubFeed(clock), crypto=crypto)
    return hub, clock


async def spot_patch(hub: LiveHub, clock: Clock) -> dict[str, Any]:
    sink = hub.connect("u")
    await hub.handle(sink, {"op": "subscribe", "topic": "event", "venue": "polymarket", "id": "bitcoin-up-or-down-5m"})
    drain(sink)
    await hub.housekeeping()
    clock.t += 1
    hub.flush()
    (patch,) = drain(sink)
    return patch["patch"]["spot"]  # type: ignore[no-any-return]


async def test_without_polybolt_the_up_down_spot_says_coinbase() -> None:
    spot = FakeSpot()
    hub, clock = updown_hub(CryptoPrices(spot, None))  # type: ignore[arg-type]
    shown = await spot_patch(hub, clock)
    assert shown["source"] == "coinbase" and shown["price"] == "77696.35" and shown["symbol"] == "BTC"
    assert spot.calls == ["BTC"]


async def test_a_polybolt_price_is_labelled_polybolt_only_when_one_exists() -> None:
    hub, clock = updown_hub(CryptoPrices(FakeSpot(), FakeBolt("77700.10")))  # type: ignore[arg-type]
    assert (await spot_patch(hub, clock))["source"] == "polybolt"
    hub, clock = updown_hub(CryptoPrices(FakeSpot(), FakeBolt(None)))  # type: ignore[arg-type]
    assert (await spot_patch(hub, clock))["source"] == "coinbase"  # no PolyBolt price yet: fall back, and say so


def test_the_service_builds_the_hub_without_a_polybolt_feed() -> None:
    from types import SimpleNamespace

    from mm_agent.domain import VenueId

    poly = SimpleNamespace(spot=FakeSpot())
    hub = build_hub(SimpleNamespace(adapters={VenueId.POLYMARKET: poly}), run_feeds=True)
    assert hub.crypto is not None and hub.crypto._bolt is None
