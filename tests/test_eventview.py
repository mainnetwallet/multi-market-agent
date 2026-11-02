from __future__ import annotations

import copy
import json
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mm_agent import eventview
from mm_agent.control_api import create_app
from mm_agent.domain import VenueId
from tests.conftest import Harness

FIX = Path(__file__).parent / "fixtures" / "live"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def load(venue: str, name: str) -> dict[str, Any]:
    body = json.loads((FIX / venue / f"{name}.json").read_text(encoding="utf-8"))
    return body["data"] if body.get("synthetic") else body  # type: ignore[no-any-return]


def pm(name: str, **kw: Any) -> dict[str, Any]:
    return eventview.build_polymarket_view(load("polymarket", name), now=NOW, **kw)


def soccer() -> dict[str, Any]:
    return pm(
        "soccer_main",
        companions=[load("polymarket", "soccer_more_markets"), load("polymarket", "soccer_halftime_result")],
    )


# ------------------------------------------------------------------------------ layouts per fixture


@pytest.mark.parametrize(
    ("name", "layout"),
    [
        ("nba_game", "game_2way"),
        ("bracket_anthropic_ipo", "bracket_ladder"),
        ("candidates", "multi_candidates"),
        ("crypto_updown_5m", "updown_window"),
        ("binary", "binary"),
    ],
)
def test_layout_per_fixture(name: str, layout: str) -> None:
    assert pm(name)["layout"] == layout


def test_soccer_is_home_draw_away_with_tabs() -> None:
    view = soccer()
    assert view["layout"] == "game_3way"
    sections = {s["id"]: s for s in view["sections"]}
    assert list(sections) == ["game_lines", "halves"]  # no empty tab, game lines first
    labels = [r["label"] for r in sections["game_lines"]["rows"] if r["market_type"] == "moneyline"]
    assert labels == ["Arsenal", "Draw", "Chelsea"]
    assert {r["market_type"] for r in sections["game_lines"]["rows"]} == {"moneyline", "totals", "both_teams_to_score"}
    assert view["status"] == "live"


def test_soccer_score_follows_ordering_not_list_order() -> None:
    raw = load("polymarket", "soccer_main")
    assert raw["teams"][0]["ordering"] == "away"  # Chelsea is listed first
    game = eventview.build_polymarket_view(raw, now=NOW)["game"]
    assert game["home"] == {"name": "Arsenal", "abbr": "ARS", "logo": "https://example.invalid/ars.png", "score": 2}
    assert game["away"]["name"] == "Chelsea"
    assert game["away"]["score"] == 1
    assert game["orientation"] == "ordering"


def test_two_way_outcome_order_differs_from_home_away() -> None:
    raw = load("polymarket", "nba_game")
    raw["markets"][0]["outcomes"] = '["Celtics", "Lakers"]'  # the home team is the first outcome here
    view = eventview.build_polymarket_view(raw, now=NOW)
    assert view["game"]["home"]["name"] == "Celtics"
    assert (view["game"]["home"]["score"], view["game"]["away"]["score"]) == (88, 91)
    rows = [r for s in view["sections"] for r in s["rows"] if r["market_type"] == "moneyline"]
    assert [r["label"] for r in rows] == ["Celtics", "Lakers"]
    assert rows[1]["yes"]["outcome"] == "NO"  # the second team's button buys the second outcome


def test_score_without_ordering_is_marked_assumed() -> None:
    raw = load("polymarket", "soccer_main")
    for t in raw["teams"]:
        del t["ordering"]
    assert eventview.build_polymarket_view(raw, now=NOW)["game"]["orientation"] == "assumed"


# ------------------------------------------------------------------------------ brackets and ordering


def test_bracket_rows_ascending_and_chart_shell() -> None:
    view = pm("bracket_anthropic_ipo")
    rows = view["sections"][0]["rows"]
    assert [r["label"] for r in rows] == ["$300B", "$400B", "$500B", "$600B"]
    chart = view["chart"]
    assert chart["point_rule"] == "midpoint"
    assert chart["ranges"] == ["1H", "6H", "1D", "1W", "1M", "ALL"]
    assert len(chart["series"]) == 4


def test_bracket_chips_from_siblings() -> None:
    sib = [{"id": "9", "slug": "anthropic-ipo-lower-strikes", "title": "Lower strikes"}]
    chips = pm("bracket_anthropic_ipo", siblings=sib)["chips"]
    assert chips == [{"id": "9", "label": "Lower strikes", "event_ref": "anthropic-ipo-lower-strikes"}]
    assert pm("bracket_anthropic_ipo")["chips"] == []  # nothing guessed


def test_candidates_sorted_by_chance_desc() -> None:
    rows = pm("candidates")["sections"][0]["rows"]
    assert [r["label"] for r in rows] == ["Alice", "Bob", "Cara"]


def test_buttons_show_ask_and_chance_is_midpoint() -> None:
    raw = load("polymarket", "binary")
    raw["markets"][0].update(bestBid="0.30", bestAsk="0.40", outcomePrices='["0.99", "0.01"]')
    row = eventview.build_polymarket_view(raw, now=NOW)["sections"][0]["rows"][0]
    assert row["chance"] == "0.35"
    assert (row["yes"]["ask"], row["yes"]["bid"]) == ("0.40", "0.30")
    assert (row["no"]["ask"], row["no"]["bid"]) == ("0.70", "0.60")


def test_prices_are_decimal_strings() -> None:
    def walk(v: Any) -> None:
        assert not isinstance(v, float)
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(soccer())
    walk(pm("bracket_anthropic_ipo"))


# ------------------------------------------------------------------------------ robustness


@pytest.mark.parametrize("name", ["soccer_main", "nba_game", "bracket_anthropic_ipo", "candidates", "binary"])
def test_shuffled_and_field_stripped_fixtures_still_render(name: str) -> None:
    raw = load("polymarket", name)
    base = eventview.build_polymarket_view(copy.deepcopy(raw), now=NOW)
    shuffled = copy.deepcopy(raw)
    random.Random(7).shuffle(shuffled["markets"])
    again = eventview.build_polymarket_view(shuffled, now=NOW)
    if base["layout"] in ("bracket_ladder", "multi_candidates"):
        assert again["sections"] == base["sections"]
    for field in ("bestBid", "bestAsk", "groupItemTitle", "oneDayPriceChange", "volume", "endDate", "icon", "image"):
        for m in raw["markets"]:
            m.pop(field, None)
    for field in ("teams", "score", "period", "tags", "image", "icon", "volume"):
        raw.pop(field, None)
    stripped = eventview.build_polymarket_view(raw, now=NOW)
    assert stripped["layout"] in eventview.LAYOUTS
    assert stripped["sections"] or stripped["layout"] == "generic_list"


def test_unknown_shape_is_generic_list_and_never_raises() -> None:
    raw = {
        "id": "7",
        "slug": "weird",
        "title": "Weird",
        "markets": [
            {"id": "w1", "question": "Three way", "outcomes": '["A","B","C"]', "outcomePrices": '["0.3","0.3","0.4"]'}
        ],
    }
    view = eventview.build_polymarket_view(raw, now=NOW)
    assert view["layout"] == "generic_list"
    assert view["sections"][0]["rows"][0]["outcomes"][2] == {"label": "C", "price": "0.4"}
    assert eventview.build_polymarket_view({"id": "8", "title": "Empty"}, now=NOW)["layout"] == "generic_list"


def test_unknown_market_type_gets_its_own_prettified_tab() -> None:
    raw = load("polymarket", "soccer_main")
    extra = {
        "id": "x",
        "slug": "epl-ars-che-2026-11-01-first-to-score",
        "markets": [dict(raw["markets"][0], id="z9", sportsMarketType="first_to_score")],
    }
    view = eventview.build_polymarket_view(raw, companions=[extra], now=NOW)
    assert "First To Score" in [s["label"] for s in view["sections"]]


def test_closed_event_status() -> None:
    raw = load("polymarket", "binary")
    raw["closed"] = True
    assert eventview.build_polymarket_view(raw, now=NOW)["status"] == "closed"


# ------------------------------------------------------------------------------ kalshi and limitless


def test_kalshi_ladder_in_contracts() -> None:
    raw = load("kalshi", "range_event")
    for m in raw["markets"]:
        m["status"] = "active"
    view = eventview.build_kalshi_view(raw, now=NOW)
    assert view["layout"] == "bracket_ladder"
    assert view["volume_unit"] == "contracts"
    rows = view["sections"][0]["rows"]
    assert [r["market_id"] for r in rows] == ["KXSYN-26NOV01-T3.0", "KXSYN-26NOV01-T3.5"]
    assert rows[0]["chance"] == "0.61"
    assert rows[0]["yes"]["ask"] == "0.62"


def test_limitless_group_children_are_rows() -> None:
    view = eventview.build_limitless_view(load("limitless", "group"), now=NOW)
    assert view["layout"] == "binary"
    assert view["sections"][0]["rows"][0]["market_id"] == "synthetic-group-a"
    assert view["url"] == "https://limitless.exchange/markets/synthetic-group"


# ------------------------------------------------------------------------------ url parser


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://polymarket.com/event/anthropic-ipo-closing-market-cap-higher-strikes", "polymarket"),
        ("https://www.polymarket.com/event/epl-ars-che-2026-11-01?tid=1", "polymarket"),
        ("https://kalshi.com/markets/kxfoo/some-title/kxfoo-26nov01", "kalshi"),
        ("https://limitless.exchange/markets/synthetic-group", "limitless"),
    ],
)
def test_parse_event_url_accepts(url: str, expected: str) -> None:
    assert eventview.parse_event_url(url)[0] == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://polymarket.com/event/abc",
        "https://evil.com/event/abc",
        "https://polymarket.com.evil.com/event/abc",
        "https://user:pw@polymarket.com/event/abc",
        "https://polymarket.com:444/event/abc",
        "https://polymarket.com/event/Bad_Slug!",
        "https://polymarket.com/event/../../etc",
        "https://polymarket.com/",
        "https://kalshi.com/markets",
        "javascript:alert(1)",
        "",
    ],
)
def test_parse_event_url_rejects(url: str) -> None:
    with pytest.raises(ValueError):
        eventview.parse_event_url(url)


def test_kalshi_ticker_is_uppercased() -> None:
    assert eventview.parse_event_url("https://kalshi.com/markets/kxfoo-26nov01") == ("kalshi", "KXFOO-26NOV01")


# ------------------------------------------------------------------------------ route


class _StubPolymarket:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def close(self) -> None:
        return None

    async def get_json(self, url: str, *, params: dict[str, Any] | None = None, headers: Any = None) -> Any:
        self.calls.append((url, params))
        if url.endswith("/events/keyset"):
            return {"events": [load("polymarket", "soccer_more_markets"), load("polymarket", "soccer_halftime_result")]}
        if params and params.get("slug") == "epl-ars-che-2026-11-01":
            return [load("polymarket", "soccer_main")]
        if params and params.get("slug") == "gone":
            return []
        return load("polymarket", "binary")


def _client(harness: Harness) -> tuple[TestClient, _StubPolymarket]:
    stub = _StubPolymarket()
    harness.registry.adapters[VenueId.POLYMARKET] = stub  # type: ignore[assignment]
    app = create_app(harness.settings, harness.agent, harness.scanner, harness.registry, harness.chat, harness.bus)
    client = TestClient(app)
    login = client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
    assert login.status_code == 200
    return client, stub


def test_route_requires_auth(harness: Harness) -> None:
    app = create_app(harness.settings, harness.agent, harness.scanner, harness.registry, harness.chat, harness.bus)
    assert TestClient(app).get("/v1/event/view?venue=polymarket&slug=x").status_code == 401


def test_route_builds_game_view_with_companions(harness: Harness) -> None:
    client, stub = _client(harness)
    r = client.get("/v1/event/view", params={"venue": "polymarket", "slug": "epl-ars-che-2026-11-01"})
    assert r.status_code == 200, r.text
    view = r.json()["view"]
    assert view["layout"] == "game_3way"
    assert [s["id"] for s in view["sections"]] == ["game_lines", "halves"]
    assert any(url.endswith("/events/keyset") for url, _ in stub.calls)


def test_route_accepts_pasted_url_and_rejects_foreign_hosts(harness: Harness) -> None:
    client, _ = _client(harness)
    ok = client.get("/v1/event/view", params={"url": "https://polymarket.com/event/epl-ars-che-2026-11-01"})
    assert ok.status_code == 200
    bad = client.get("/v1/event/view", params={"url": "https://evil.com/event/epl-ars-che-2026-11-01"})
    assert bad.status_code == 400


def test_route_404_for_missing_event_and_400_for_bad_slug(harness: Harness) -> None:
    client, _ = _client(harness)
    assert client.get("/v1/event/view", params={"venue": "polymarket", "slug": "gone"}).status_code == 404
    assert client.get("/v1/event/view", params={"venue": "polymarket", "slug": "Bad Slug"}).status_code == 400
    assert client.get("/v1/event/view", params={"venue": "polymarket"}).status_code == 400


def test_route_disabled_by_flag(harness: Harness) -> None:
    _client(harness)
    off = harness.settings.model_copy(update={"live_views_enabled": False})
    client = TestClient(create_app(off, harness.agent, harness.scanner, harness.registry, harness.chat, harness.bus))
    client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
    assert client.get("/v1/event/view", params={"venue": "polymarket", "slug": "x"}).status_code == 404
