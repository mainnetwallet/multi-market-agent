from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from mm_agent import discover
from mm_agent.venues.polymarket import PolymarketVenue
from tests.conftest import make_settings


def _market(mid: str, yes: str, no: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": mid,
        "question": f"Q{mid}?",
        "groupItemTitle": f"Opt {mid}",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": f'["{yes}", "{no}"]',
        "clobTokenIds": f'["t{mid}a", "t{mid}b"]',
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "volume": "1500.5",
        "oneDayPriceChange": 0.04,
        "endDate": "2026-12-01T00:00:00Z",
    }
    base.update(extra)
    return base


def _event(eid: str, markets: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": eid,
        "title": f"Event {eid}",
        "slug": f"event-{eid}",
        "image": "https://img/x.png",
        "volume": 9000,
        "volume24hr": 120.5,
        "liquidity": 40,
        "tags": [{"label": "Politics"}],
        "markets": markets,
    }
    base.update(extra)
    return base


def test_shape_event_sorts_and_filters() -> None:
    event = _event(
        "1",
        [
            _market("a", "0.2", "0.8"),
            _market("b", "0.7", "0.3"),
            _market("c", "0.5", "0.5", closed=True),
            _market("d", "0.5", "0.5", outcomes='["Up", "Down", "Flat"]'),
        ],
    )
    card = discover.shape_event(event)
    assert card is not None
    assert [m["market_id"] for m in card["markets"]] == ["b", "a"]
    assert card["market_count"] == 2
    assert card["tags"] == ["Politics"]
    assert card["url"] == "https://polymarket.com/event/event-1"
    assert card["markets"][0]["yes_price"] == "0.7"


def test_shape_events_drops_closed_and_dupes() -> None:
    one = _event("1", [_market("a", "0.2", "0.8")])
    rows = [one, one, _event("2", [], title="x")]
    rows.append(_event("3", [_market("z", "0.1", "0.9")], closed=True))
    assert [e["event_id"] for e in discover.shape_events(rows)] == ["1"]
    assert discover.shape_events({"not": "a list"}) == []


def test_breaking_ranks_by_move() -> None:
    items = discover.shape_events(
        [
            _event("1", [_market("a", "0.2", "0.8", oneDayPriceChange=0.01)]),
            _event("2", [_market("b", "0.2", "0.8", oneDayPriceChange=-0.30)]),
        ]
    )
    assert [i["event_id"] for i in discover.rank_by_change(items)] == ["2", "1"]


def test_tabs_cover_requested_categories() -> None:
    ids = {t["id"] for t in discover.tab_list()}
    for want in (
        "trending",
        "combos",
        "perps",
        "breaking",
        "new",
        "midterms",
        "politics",
        "sports",
        "crypto",
        "esports",
        "iran",
        "finance",
        "geopolitics",
        "tech",
    ):
        assert want in ids
    flags = {t["id"]: t["supported"] for t in discover.tab_list()}
    assert flags["combos"] is False and flags["perps"] is False and flags["politics"] is True


def test_discover_calls_gamma_with_tag_and_warms_cache(tmp_path: Path) -> None:
    venue = PolymarketVenue(make_settings(tmp_path))
    seen: dict[str, Any] = {}

    async def fake_get_json(url: str, *, params: dict[str, Any] | None = None, headers: Any = None) -> Any:
        seen["url"], seen["params"] = url, params
        return [_event("1", [_market("a", "0.2", "0.8")])]

    venue.get_json = fake_get_json  # type: ignore[method-assign]
    out = asyncio.run(venue.discover(tab="politics", q=None, limit=10, offset=0))
    assert seen["url"].endswith("/events")
    assert seen["params"]["tag_slug"] == "politics"
    assert seen["params"]["order"] == "volume24hr"
    assert out["items"][0]["markets"][0]["market_id"] == "a"
    assert "a" in venue._markets  # the trade sheet can open it without another lookup

    out = asyncio.run(venue.discover(tab="perps", q=None, limit=10, offset=0))
    assert out["items"] == [] and out["note"]
    with pytest.raises(ValueError):
        asyncio.run(venue.discover(tab="nope", q=None, limit=10, offset=0))


def test_discover_search_uses_public_search(tmp_path: Path) -> None:
    venue = PolymarketVenue(make_settings(tmp_path))
    seen: dict[str, Any] = {}

    async def fake_get_json(url: str, *, params: dict[str, Any] | None = None, headers: Any = None) -> Any:
        seen["url"], seen["params"] = url, params
        return {"events": [_event("9", [_market("s", "0.3", "0.7")])], "pagination": {"hasMore": True}}

    venue.get_json = fake_get_json  # type: ignore[method-assign]
    out = asyncio.run(venue.discover(tab="trending", q="newsom", limit=10, offset=0))
    assert seen["url"].endswith("/public-search") and seen["params"]["q"] == "newsom"
    assert out["items"][0]["event_id"] == "9" and out["next_offset"] == 10
