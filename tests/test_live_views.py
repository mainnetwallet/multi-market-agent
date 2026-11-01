from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from mm_agent import discover
from mm_agent.crypto_feed import SpotFeed
from mm_agent.venues.polymarket import PolymarketVenue
from tests.conftest import make_settings


def _market(mid: str, outcomes: str, prices: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": mid,
        "question": f"Q{mid}",
        "outcomes": outcomes,
        "outcomePrices": prices,
        "clobTokenIds": f'["t{mid}a", "t{mid}b"]',
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "volume": "10",
        "endDate": "2026-11-02T00:00:00Z",
    }
    base.update(extra)
    return base


def _tennis(**extra: Any) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "id": "900",
        "title": "Leylah Fernandez - Diana Shnaider",
        "slug": "wta-fernandez-shnaider-2026-11-01",
        "seriesSlug": "wta",
        "live": True,
        "ended": False,
        "period": "S2",
        "score": "4-6, 0-3",
        "startTime": "2026-11-01T04:00:00Z",
        "volume": 1240000,
        "tags": [{"label": "Sports"}],
        "markets": [
            _market("m1", '["Leylah Fernandez", "Diana Shnaider"]', '["0.098", "0.902"]', sportsMarketType="moneyline"),
            _market("m2", '["Over", "Under"]', '["0.5", "0.5"]', sportsMarketType="totals"),
        ],
    }
    ev.update(extra)
    return ev


def _btc(**extra: Any) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "id": "901",
        "title": "Bitcoin Up or Down - November 1, 12:25AM-12:30AM ET",
        "slug": "btc-updown-5m-1761971100",
        "volume": 5000,
        "markets": [_market("c1", '["Up", "Down"]', '["0.6", "0.4"]')],
    }
    ev.update(extra)
    return ev


def test_parse_score_sets_and_fallback() -> None:
    assert discover.parse_score("4-6, 0-3") == {"raw": "4-6, 0-3", "sets": [[4, 6], [0, 3]]}
    assert discover.parse_score("102-98") == {"raw": "102-98", "sets": [[102, 98]]}
    assert discover.parse_score("1:0") == {"raw": "1:0", "sets": [[1, 0]]}
    assert discover.parse_score("Q3 12:00") == {"raw": "Q3 12:00", "sets": []}  # unknown format: raw text only
    assert discover.parse_score("") is None
    assert discover.parse_score(None) is None


def test_sport_event_has_teams_score_and_live_flags() -> None:
    card = discover.shape_event(_tennis())
    assert card is not None and card["kind"] == "sport" and card["crypto"] is None
    sp = card["sport"]
    assert sp["market_id"] == "m1" and sp["league"] == "wta" and sp["live"] is True and sp["period"] == "S2"
    assert [t["name"] for t in sp["teams"]] == ["Leylah Fernandez", "Diana Shnaider"]
    assert [t["outcome"] for t in sp["teams"]] == ["YES", "NO"]
    assert sp["teams"][1]["price"] == "0.902"
    assert sp["score"]["sets"] == [[4, 6], [0, 3]]


def test_plain_event_stays_a_market() -> None:
    ev = {
        "id": "1",
        "title": "Will it rain?",
        "slug": "rain",
        "markets": [_market("a", '["Yes", "No"]', '["0.2", "0.8"]')],
    }
    card = discover.shape_event(ev)
    assert card is not None and card["kind"] == "market" and card["sport"] is None and card["crypto"] is None


def test_yes_no_totals_alone_is_not_a_game() -> None:
    # a sports-looking market whose outcomes are not two teams gives no team card
    ev = _tennis(markets=[_market("m2", '["Over", "Under"]', '["0.5", "0.5"]', sportsMarketType="totals")])
    card = discover.shape_event(ev)
    assert card is not None and card["kind"] == "market"


def test_crypto_window_from_slug() -> None:
    card = discover.shape_event(_btc())
    assert card is not None and card["kind"] == "crypto_updown"
    c = card["crypto"]
    assert c["symbol"] == "BTC" and c["name"] == "Bitcoin"
    assert c["start_ts"] == 1761971100 and c["end_ts"] == 1761971100 + 300 and c["span_seconds"] == 300
    assert c["up_price"] == "0.6" and c["down_price"] == "0.4"
    assert c["price_to_beat"] is None and c["price_to_beat_source"] is None
    assert [w["slug"] for w in c["windows"]][:2] == ["btc-updown-5m-1761971400", "btc-updown-5m-1761971700"]


def test_crypto_price_to_beat_used_when_polymarket_sends_it() -> None:
    card = discover.shape_event(_btc(eventMetadata={"priceToBeat": 77672.35}))
    assert card is not None
    assert card["crypto"]["price_to_beat"] == "77672.35" and card["crypto"]["price_to_beat_source"] == "polymarket"


def test_crypto_from_title_when_slug_differs() -> None:
    ev = _btc(slug="something-else", startTime="2026-11-01T04:25:00Z", endDate="2026-11-01T04:30:00Z")
    card = discover.shape_event(ev)
    assert card is not None and card["kind"] == "crypto_updown" and card["crypto"]["span_seconds"] == 300
    assert card["crypto"]["windows"] == []  # no slug pattern, so no sibling windows are guessed


def test_closed_event_needs_allow_closed() -> None:
    ev = _tennis(closed=True, ended=True, live=False, markets=[_market("m1", '["A", "B"]', '["1", "0"]', closed=True)])
    assert discover.shape_event(ev) is None
    card = discover.shape_event(ev, allow_closed=True)
    assert card is not None and card["sport"]["ended"] is True


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_spot_feed_price_ticks_and_window_start() -> None:
    clock = _Clock(1_000_030.0)
    calls: list[str] = []

    async def fetch(url: str, **kw: Any) -> Any:
        calls.append(url)
        if "spot" in url:
            return {"data": {"amount": "77696.35"}}
        # Coinbase candles: [time, low, high, open, close, volume], newest first
        return [[1_000_020, 1, 2, "77700", "77701", 1], [1_000_020 - 60, 1, 2, "77672.35", "77680", 1]]

    feed = SpotFeed(fetch, clock=clock)
    snap = asyncio.run(feed.snapshot("btc", window_start=1_000_020 - 60))
    assert snap["price"] == "77696.35" and snap["source"] == "coinbase" and snap["symbol"] == "BTC"
    assert snap["start_price"] == "77672.35"  # open of the candle at the window start
    assert snap["candles"][0][0] < snap["candles"][1][0] and len(snap["ticks"]) == 1
    n = len(calls)
    asyncio.run(feed.snapshot("BTC"))  # inside the 1 s gap and the candle cache: no new requests
    assert len(calls) == n


def test_spot_feed_rejects_bad_symbol_and_bad_payload() -> None:
    async def fetch(url: str, **kw: Any) -> Any:
        return {"data": {"amount": "nope"}}

    feed = SpotFeed(fetch)
    with pytest.raises(ValueError):
        asyncio.run(feed.snapshot("../etc"))
    with pytest.raises(ValueError):
        asyncio.run(feed.snapshot("BTC"))


def test_event_live_looks_up_by_id_and_slug(tmp_path: Path) -> None:
    venue = PolymarketVenue(make_settings(tmp_path))
    seen: list[tuple[str, Any]] = []

    async def fake_get_json(url: str, *, params: Any = None, headers: Any = None) -> Any:
        seen.append((url, params))
        return _tennis() if url.endswith("/events/900") else [_btc()]

    venue.get_json = fake_get_json  # type: ignore[method-assign]
    item = asyncio.run(venue.event_live("900"))
    assert item is not None and item["kind"] == "sport"
    item = asyncio.run(venue.event_live(slug="btc-updown-5m-1761971100"))
    assert item is not None and item["kind"] == "crypto_updown"
    assert seen[1][1] == {"slug": "btc-updown-5m-1761971100"}
    assert "c1" in venue._markets  # remembered, so the trade sheet opens without another lookup
    with pytest.raises(ValueError):
        asyncio.run(venue.event_live("9/../x"))
    with pytest.raises(ValueError):
        asyncio.run(venue.event_live(slug="BAD SLUG"))


def test_spot_feed_is_decimal_exact() -> None:
    async def fetch(url: str, **kw: Any) -> Any:
        return {"data": {"amount": "0.1"}} if "spot" in url else []

    snap = asyncio.run(SpotFeed(fetch).snapshot("ETH"))
    assert Decimal(snap["price"]) == Decimal("0.1") and snap["candles"] == []
