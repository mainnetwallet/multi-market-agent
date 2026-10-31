from __future__ import annotations

import asyncio
import hashlib
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


def test_polymarket_static_tabs_are_only_the_sort_modes() -> None:
    assert [t["id"] for t in discover.tab_list()] == ["trending", "breaking", "new"]


def test_tag_tabs_come_from_gamma_rows() -> None:
    rows = [
        {"id": "2", "label": "Politics", "slug": "politics", "isCarousel": True},
        {"id": "3", "label": "Hidden", "slug": "hidden", "forceHide": True},
        {"id": "4", "label": "", "slug": "nolabel"},
        {"id": "5", "label": "Same as sort", "slug": "new"},  # must not shadow the New tab
        {"id": "6", "label": "Politics again", "slug": "politics"},
        {"id": "7", "label": "Culture", "slug": "pop-culture"},
        "junk",
    ]
    assert discover.tag_tabs(rows) == [
        {"id": "politics", "label": "Politics", "tag_id": "2"},
        {"id": "pop-culture", "label": "Culture", "tag_id": "7"},
    ]
    assert discover.tag_tabs({"not": "a list"}) == []


def test_discover_tabs_read_gamma_tags_and_cache(tmp_path: Path) -> None:
    venue = PolymarketVenue(make_settings(tmp_path))
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_get_json(url: str, *, params: dict[str, Any] | None = None, headers: Any = None) -> Any:
        calls.append((url, params))
        return [{"id": "7", "label": "Culture", "slug": "pop-culture", "isCarousel": True}]

    venue.get_json = fake_get_json  # type: ignore[method-assign]
    out = asyncio.run(venue.discover_tabs())
    assert [t["id"] for t in out["tabs"]] == ["trending", "breaking", "new", "pop-culture"]
    assert out["tabs"][-1]["label"] == "Culture" and out["note"] == ""
    tag_calls = [c for c in calls if c[0].endswith("/tags")]
    assert len(tag_calls) == 1 and tag_calls[0][1] is not None and tag_calls[0][1]["is_carousel"] == "true"
    before = len(calls)
    asyncio.run(venue.discover_tabs())
    assert len(calls) == before  # kept for hours


def _tagged(event_id: str, *tags: dict[str, Any]) -> dict[str, Any]:
    return {"id": event_id, "tags": list(tags)}


def _tag(tag_id: str, label: str, slug: str, **extra: Any) -> dict[str, Any]:
    return {"id": tag_id, "label": label, "slug": slug, **extra}


def test_event_tag_tabs_rank_by_how_many_events_carry_the_tag() -> None:
    politics, sports, crypto = (
        _tag("2", "Politics", "politics"),
        _tag("1", "Sports", "sports"),
        _tag("21", "Crypto", "crypto"),
    )
    events = [
        _tagged("a", politics, sports),
        _tagged("b", sports, crypto),
        _tagged("c", sports),
        _tagged("d", politics, _tag("8", "Hidden", "hidden", forceHide=True)),
        _tagged("e", _tag("5", "Same as sort", "new"), _tag("6", "Once", "once")),  # sort-mode slug; seen once
        "junk",
    ]
    assert discover.event_tag_tabs(events, exclude={"trending", "breaking", "new"}) == [
        {"id": "sports", "label": "Sports", "tag_id": "1"},
        {"id": "politics", "label": "Politics", "tag_id": "2"},
    ]
    assert discover.event_tag_tabs(events, min_events=1, limit=1) == [
        {"id": "sports", "label": "Sports", "tag_id": "1"}
    ]
    assert discover.event_tag_tabs({"not": "a list"}) == []


def test_discover_tabs_combine_event_tags_and_carousel_tags(tmp_path: Path) -> None:
    venue = PolymarketVenue(make_settings(tmp_path))
    politics, sports = _tag("2", "Politics", "politics"), _tag("1", "Sports", "sports")

    async def fake_get_json(url: str, *, params: dict[str, Any] | None = None, headers: Any = None) -> Any:
        if url.endswith("/tags"):
            return [_tag("42", "AI", "ai", isCarousel=True), _tag("2", "Politics", "politics", isCarousel=True)]
        return [_tagged("a", politics, sports), _tagged("b", sports, politics), _tagged("c", sports)]

    venue.get_json = fake_get_json  # type: ignore[method-assign]
    out = asyncio.run(venue.discover_tabs())
    assert [t["id"] for t in out["tabs"]] == ["trending", "breaking", "new", "sports", "politics", "ai"]
    assert out["note"] == ""


def test_discover_tabs_keep_carousel_tags_when_events_fail(tmp_path: Path) -> None:
    venue = PolymarketVenue(make_settings(tmp_path))

    async def fake_get_json(url: str, *, params: dict[str, Any] | None = None, headers: Any = None) -> Any:
        if url.endswith("/tags"):
            return [_tag("42", "AI", "ai")]
        raise RuntimeError("events down")

    venue.get_json = fake_get_json  # type: ignore[method-assign]
    out = asyncio.run(venue.discover_tabs())
    assert [t["id"] for t in out["tabs"]][-1] == "ai"
    assert "events down" in out["note"]


def test_discover_tabs_failure_keeps_sort_modes_and_says_why(tmp_path: Path) -> None:
    venue = PolymarketVenue(make_settings(tmp_path))

    async def boom(url: str, *, params: dict[str, Any] | None = None, headers: Any = None) -> Any:
        raise RuntimeError("gamma down")

    venue.get_json = boom  # type: ignore[method-assign]
    out = asyncio.run(venue.discover_tabs())
    assert [t["id"] for t in out["tabs"]] == ["trending", "breaking", "new"]
    assert "gamma down" in out["note"]


def test_discover_uses_the_tag_id_gamma_gave(tmp_path: Path) -> None:
    venue = PolymarketVenue(make_settings(tmp_path))
    seen: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_get_json(url: str, *, params: dict[str, Any] | None = None, headers: Any = None) -> Any:
        seen.append((url, params))
        if url.endswith("/tags"):
            return [{"id": "7", "label": "Culture", "slug": "pop-culture"}]
        return [_event("1", [_market("a", "0.2", "0.8")])]

    venue.get_json = fake_get_json  # type: ignore[method-assign]
    out = asyncio.run(venue.discover(tab="pop-culture", q=None, limit=10, offset=0))
    url, params = seen[-1]
    assert url.endswith("/events") and params is not None
    assert params["tag_id"] == "7" and "tag_slug" not in params
    assert params["order"] == "volume24hr"
    assert out["items"][0]["markets"][0]["market_id"] == "a"
    assert "a" in venue._markets  # the trade sheet can open it without another lookup

    with pytest.raises(ValueError):
        asyncio.run(venue.discover(tab="nope", q=None, limit=10, offset=0))
    with pytest.raises(ValueError):
        asyncio.run(venue.discover(tab="perps", q=None, limit=10, offset=0))  # no longer a built-in tab


def test_discover_empty_category_says_so(tmp_path: Path) -> None:
    venue = PolymarketVenue(make_settings(tmp_path))

    async def fake_get_json(url: str, *, params: dict[str, Any] | None = None, headers: Any = None) -> Any:
        return [{"id": "7", "label": "Culture", "slug": "pop-culture"}] if url.endswith("/tags") else []

    venue.get_json = fake_get_json  # type: ignore[method-assign]
    out = asyncio.run(venue.discover(tab="pop-culture", q=None, limit=10, offset=0))
    assert out["items"] == [] and out["note"]


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


# ------------------------------------------------------------------ Kalshi / Limitless


def _kmarket(ticker: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ticker": ticker,
        "title": f"Will {ticker} happen?",
        "yes_sub_title": f"Opt {ticker}",
        "market_type": "binary",
        "status": "active",
        "yes_bid_dollars": "0.40",
        "yes_ask_dollars": "0.44",
        "no_ask_dollars": "0.60",
        "last_price_dollars": "0.42",
        "previous_price_dollars": "0.38",
        "volume_fp": "1000",
        "volume_24h_fp": "120",
        "close_time": "2026-12-01T00:00:00Z",
        "created_time": "2026-10-01T00:00:00Z",
    }
    base.update(extra)
    return base


def _kevent(ticker: str, markets: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "event_ticker": ticker,
        "title": f"Event {ticker}",
        "category": "Politics",
        "markets": markets,
    }
    base.update(extra)
    return base


def test_kalshi_card_prices_and_move() -> None:
    card = discover.shape_kalshi_event(_kevent("E1", [_kmarket("A")]))
    assert card is not None
    row = card["markets"][0]
    assert row["yes_price"] == "0.44" and row["no_price"] == "0.60"
    assert row["chance"] == "0.42"  # mid of 0.40/0.44
    assert row["change_1d"] == "0.04"
    assert card["volume_unit"] == "contracts"
    assert card["url"] == "https://kalshi.com/markets/e1"


def test_kalshi_skips_combos_closed_and_unpriced() -> None:
    event = _kevent(
        "E2",
        [
            _kmarket("A", mve_collection_ticker="X"),
            _kmarket("B", status="closed"),
            _kmarket("C", yes_bid_dollars="", yes_ask_dollars="", last_price_dollars=""),
            _kmarket("D", market_type="scalar"),
        ],
    )
    assert discover.shape_kalshi_event(event) is None


def test_kalshi_category_filter_and_search() -> None:
    rows = [
        _kevent("P", [_kmarket("P1")], category="Politics"),
        _kevent("S", [_kmarket("S1")], category="Sports", title="Super game"),
        _kevent("T", [_kmarket("T1")], category="Science and Technology"),
    ]
    assert [e["event_id"] for e in discover.shape_kalshi_events(rows, match=["sport"])] == ["S"]
    assert [e["event_id"] for e in discover.shape_kalshi_events(rows, match=["tech", "science"])] == ["T"]
    assert [e["event_id"] for e in discover.shape_kalshi_events(rows, q="super")] == ["S"]
    # A category tab keeps events whose category is exactly the name Kalshi listed, in any letter case.
    assert [e["event_id"] for e in discover.shape_kalshi_events(rows, category="science and technology")] == ["T"]
    assert discover.shape_kalshi_events(rows, category="Science") == []


def test_kalshi_category_tabs_come_from_tags_by_categories() -> None:
    data = {
        "tags_by_categories": {
            "Politics": ["Trump"],
            "Science and Technology": [],
            "New": ["x"],  # must not shadow the New sort tab
            "": ["junk"],
        }
    }
    assert discover.kalshi_category_tabs(data) == [
        {"id": "politics", "label": "Politics", "category": "Politics"},
        {"id": "science-and-technology", "label": "Science and Technology", "category": "Science and Technology"},
    ]
    assert discover.kalshi_category_tabs({"tags_by_categories": []}) == []
    assert discover.kalshi_category_tabs("nope") == []


def test_rank_items_volume_and_newest() -> None:
    items = [
        {"event_id": "a", "volume_24h": "5", "volume": "1", "created": "2026-01-01"},
        {"event_id": "b", "volume_24h": "9", "volume": "1", "created": "2026-02-01"},
    ]
    assert [i["event_id"] for i in discover.rank_items(items, "volume")] == ["b", "a"]
    assert [i["event_id"] for i in discover.rank_items(items, "newest")] == ["b", "a"]
    assert discover.rank_items(items, None) == items


def test_kalshi_discover_pages_with_cursor(tmp_path: Path) -> None:
    from mm_agent.venues.kalshi import KalshiVenue

    venue = KalshiVenue(make_settings(tmp_path))
    calls: list[dict[str, Any]] = []

    async def fake_get_json(url: str, *, params: dict[str, Any] | None = None, headers: Any = None) -> Any:
        if url.endswith("/search/tags_by_categories"):
            return {"tags_by_categories": {"Politics": []}}
        calls.append(dict(params or {}))
        n = len(calls)
        return {"events": [_kevent(f"E{n}", [_kmarket(f"M{n}")])], "cursor": f"c{n}" if n < 9 else ""}

    venue.get_json = fake_get_json  # type: ignore[method-assign]
    out = asyncio.run(venue.discover(tab="politics", q=None, limit=1, offset=0))
    assert calls[0]["status"] == "open" and calls[0]["with_nested_markets"] == "true"
    assert [i["event_id"] for i in out["items"]] == ["E1"]
    nxt = out["next_offset"]
    assert nxt is not None
    calls.clear()
    out = asyncio.run(venue.discover(tab="politics", q=None, limit=1, offset=nxt))
    assert calls[0]["cursor"] == "c1"  # resumed where the first page stopped
    # An offset the server never issued ends the list instead of restarting it.
    stale = asyncio.run(venue.discover(tab="politics", q=None, limit=1, offset=999))
    assert stale["items"] == [] and stale["next_offset"] is None
    with pytest.raises(ValueError):
        asyncio.run(venue.discover(tab="nope", q=None, limit=1, offset=0))


def test_kalshi_discover_tabs_and_category_tab(tmp_path: Path) -> None:
    from mm_agent.venues.kalshi import KalshiVenue

    venue = KalshiVenue(make_settings(tmp_path))
    urls: list[str] = []

    async def fake_get_json(url: str, *, params: dict[str, Any] | None = None, headers: Any = None) -> Any:
        urls.append(url)
        if url.endswith("/search/tags_by_categories"):
            return {"tags_by_categories": {"Politics": [], "Sports": []}}
        return {
            "events": [
                _kevent("P", [_kmarket("P1")], category="Politics"),
                _kevent("S", [_kmarket("S1")], category="Sports"),
            ],
            "cursor": "",
        }

    venue.get_json = fake_get_json  # type: ignore[method-assign]
    tabs = asyncio.run(venue.discover_tabs())
    assert [t["id"] for t in tabs["tabs"]] == ["trending", "new", "politics", "sports"] and tabs["note"] == ""
    out = asyncio.run(venue.discover(tab="sports", q=None, limit=10, offset=0))
    assert [i["event_id"] for i in out["items"]] == ["S"]
    assert sum(u.endswith("/search/tags_by_categories") for u in urls) == 1  # kept for hours
    with pytest.raises(ValueError):
        asyncio.run(venue.discover(tab="perps", q=None, limit=10, offset=0))


def test_kalshi_discover_tabs_failure_says_why(tmp_path: Path) -> None:
    from mm_agent.venues.kalshi import KalshiVenue

    venue = KalshiVenue(make_settings(tmp_path))

    async def boom(url: str, *, params: dict[str, Any] | None = None, headers: Any = None) -> Any:
        raise RuntimeError("kalshi down")

    venue.get_json = boom  # type: ignore[method-assign]
    tabs = asyncio.run(venue.discover_tabs())
    assert [t["id"] for t in tabs["tabs"]] == ["trending", "new"] and "kalshi down" in tabs["note"]


def _lmarket(slug: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": int.from_bytes(hashlib.sha256(slug.encode()).digest()[:4], "big"),
        "slug": slug,
        "title": f"Market {slug}",
        "status": "FUNDED",
        "expired": False,
        "tradeType": "clob",
        "prices": [0.62, 0.38],
        "volumeFormatted": "141.27",
        "expirationTimestamp": 1793349900000,
        "categories": ["Crypto", "Bitcoin"],
        "imageUrl": "https://img/x.png",
        "createdAt": "2026-10-30T08:00:00.000Z",
        "tokens": {"yes": "1", "no": "2"},
    }
    base.update(extra)
    return base


def test_limitless_card_single_and_group() -> None:
    single = discover.shape_limitless_event(_lmarket("a"))
    assert single is not None
    assert single["markets"][0]["yes_price"] == "0.62"
    assert single["volume"] == "141.27"
    assert single["image"] == "https://img/x.png"
    assert single["end_time"] == "2026-10-30T08:45:00Z"
    group = discover.shape_limitless_event(
        _lmarket("grp", groupId="g1", markets=[_lmarket("x", prices=[0.2, 0.8]), _lmarket("y", prices=[0.7, 0.3])])
    )
    assert group is not None and group["event_id"] == "g1"
    assert [m["market_id"] for m in group["markets"]] == ["y", "x"]


def test_limitless_skips_untradable() -> None:
    for bad in (
        {"expired": True},
        {"hidden": True},
        {"tradeType": "amm"},
        {"status": "RESOLVED"},
        {"prices": [0.5]},
        {"prices": [2, -1]},
    ):
        assert discover.shape_limitless_event(_lmarket("z", **bad)) is None


def test_limitless_category_filter() -> None:
    rows = [_lmarket("c"), _lmarket("s", categories=["Sports"])]
    got = discover.shape_limitless_events(rows, match=["sport"])
    assert [e["slug"] for e in got] == ["s"]


def test_limitless_discover_pages_and_warms(tmp_path: Path) -> None:
    from mm_agent.venues.limitless import LimitlessVenue

    venue = LimitlessVenue(make_settings(tmp_path))
    seen: list[tuple[str, dict[str, Any]]] = []

    async def fake_get_json(url: str, *, params: dict[str, Any] | None = None, headers: Any = None) -> Any:
        seen.append((url, dict(params or {})))
        page = (params or {})["page"]
        return {"data": [_lmarket(f"m{page}-{i}") for i in range(25)], "totalMarketsCount": 400}

    venue.get_json = fake_get_json  # type: ignore[method-assign]
    out = asyncio.run(venue.discover(tab="trending", q=None, limit=30, offset=0))
    assert [p["page"] for _, p in seen] == [1, 2]
    assert all(p["limit"] == 25 and p["sortBy"] == "volume" for _, p in seen)
    assert len(out["items"]) == 50 and out["next_offset"] == 50
    assert "m1-0" in venue._markets
    seen.clear()
    asyncio.run(venue.discover(tab="trending", q=None, limit=30, offset=50))
    assert [p["page"] for _, p in seen] == [3, 4]
    seen.clear()
    with pytest.raises(ValueError):
        asyncio.run(venue.discover(tab="nope", q=None, limit=30, offset=0))


def test_limitless_category_helpers() -> None:
    data = {"category": {"3": 8, "1": 10, "2": 0, "x": 4}, "totalCount": 22}
    assert discover.limitless_category_counts(data) == [("1", 10), ("3", 8)]  # busiest first, empty/odd ids dropped
    assert discover.limitless_category_counts({"category": []}) == []
    assert discover.limitless_category_name([{"categories": ["Crypto", "Hourly"]}]) == "Crypto"
    assert discover.limitless_category_name([{"categories": []}, {"categories": ["Sports"]}]) == "Sports"
    assert discover.limitless_category_name("nope") == ""


def test_limitless_category_tabs_use_the_documented_id_to_name_lookup(tmp_path: Path) -> None:
    from mm_agent.venues.limitless import LimitlessVenue

    venue = LimitlessVenue(make_settings(tmp_path))
    seen: list[tuple[str, dict[str, Any]]] = []

    async def fake_get_json(url: str, *, params: dict[str, Any] | None = None, headers: Any = None) -> Any:
        seen.append((url, dict(params or {})))
        if url.endswith("/markets/categories/count"):
            return {"category": {"1": 10, "2": 5, "9": 3}, "totalCount": 18}
        if url.endswith("/markets/active/1") and (params or {}).get("limit") == 1:
            return {"data": [_lmarket("a", categories=["Crypto"])]}
        if url.endswith("/markets/active/2"):
            return {"data": [_lmarket("b", categories=["Sports"])]}
        if url.endswith("/markets/active/9"):
            raise RuntimeError("no name for this one")
        return {"data": [_lmarket(f"c{(params or {}).get('page')}-{i}") for i in range(25)]}

    venue.get_json = fake_get_json  # type: ignore[method-assign]
    out = asyncio.run(venue.discover_tabs())
    ids = [(t["id"], t["label"]) for t in out["tabs"]]
    assert ids == [("trending", "Trending"), ("new", "New"), ("liquid", "Liquid"), ("1", "Crypto"), ("2", "Sports")]
    assert [p["limit"] for u, p in seen if u.endswith(("/active/1", "/active/2"))] == [1, 1]
    n = len(seen)
    asyncio.run(venue.discover_tabs())
    assert len(seen) == n  # the id -> name mapping is kept for hours

    seen.clear()
    res = asyncio.run(venue.discover(tab="1", q=None, limit=30, offset=0))
    assert [u for u, _ in seen] == [venue.base_url + "/markets/active/1"] * 2  # the venue filters, not us
    assert all(p["sortBy"] == "volume" and p["limit"] == 25 for _, p in seen)
    assert len(res["items"]) == 50
    with pytest.raises(ValueError):
        asyncio.run(venue.discover(tab="77", q=None, limit=30, offset=0))  # not one of the listed categories


def test_limitless_tabs_failure_keeps_sort_modes(tmp_path: Path) -> None:
    from mm_agent.venues.limitless import LimitlessVenue

    venue = LimitlessVenue(make_settings(tmp_path))

    async def boom(url: str, *, params: dict[str, Any] | None = None, headers: Any = None) -> Any:
        raise RuntimeError("limitless down")

    venue.get_json = boom  # type: ignore[method-assign]
    out = asyncio.run(venue.discover_tabs())
    assert [t["id"] for t in out["tabs"]] == ["trending", "new", "liquid"] and "limitless down" in out["note"]


def test_limitless_search_falls_back_to_active_list(tmp_path: Path) -> None:
    from mm_agent.venues.base import VenueHTTPError
    from mm_agent.venues.limitless import LimitlessVenue

    venue = LimitlessVenue(make_settings(tmp_path))

    async def fake_get_json(url: str, *, params: dict[str, Any] | None = None, headers: Any = None) -> Any:
        if url.endswith("/markets/search"):
            raise VenueHTTPError(404, "nope", url)
        return {"data": [_lmarket("btc", title="Bitcoin above 100k"), _lmarket("eth", title="Ether flips")]}

    venue.get_json = fake_get_json  # type: ignore[method-assign]
    out = asyncio.run(venue.discover(tab="trending", q="bitcoin", limit=30, offset=0))
    assert [i["slug"] for i in out["items"]] == ["btc"]


def test_tab_lists_per_venue() -> None:
    # Only sort modes are fixed; every category comes from the venue's own API at runtime.
    assert [t["id"] for t in discover.tab_list("kalshi")] == ["trending", "new"]
    assert [t["id"] for t in discover.tab_list("limitless")] == ["trending", "new", "liquid"]
    assert discover.tab_list("fake") == []
    assert all(t["supported"] for t in discover.tab_list("kalshi") + discover.tab_list("limitless"))
