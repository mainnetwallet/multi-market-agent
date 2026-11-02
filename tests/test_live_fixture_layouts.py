"""Layouts for the second set of synthetic fixtures (tennis, MMA, esports, mentions, weather, Kalshi, Limitless).

Every fixture here is hand-built from documented shapes and carries ``"synthetic": true``; none has been seen on a
live response (``LIVE_VERIFIED=false``). The tests pin what the builders do with those shapes, so a real capture
that disagrees shows up as a failing test instead of a silent change in the app.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from mm_agent import eventview
from tests.test_eventview import FIX, NOW, load

POLYMARKET_LAYOUTS = [
    ("tennis_match", "game_2way"),
    ("mma_fight", "game_2way"),
    ("esports_match", "game_2way"),
    ("mentions", "multi_candidates"),
    ("weather_temperature", "bracket_ladder"),
]
KALSHI_LAYOUTS = [("sports_event", "multi_candidates"), ("binary_event", "binary")]
LIMITLESS_LAYOUTS = [("clob_market", "binary"), ("recurring_market", "binary")]


def pm(name: str, **kw: Any) -> dict[str, Any]:
    return eventview.build_polymarket_view(load("polymarket", name), now=NOW, **kw)


def kalshi(name: str) -> dict[str, Any]:
    return eventview.build_kalshi_view(load("kalshi", name), now=NOW)


def limitless(name: str) -> dict[str, Any]:
    return eventview.build_limitless_view(load("limitless", name), now=NOW)


def rows_of(view: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for s in view["sections"] for r in s["rows"]]


# ------------------------------------------------------------------------------------------ one layout per fixture
@pytest.mark.parametrize(("name", "layout"), POLYMARKET_LAYOUTS)
def test_polymarket_fixture_layout(name: str, layout: str) -> None:
    view = pm(name)
    assert view["layout"] == layout and view["venue"] == "polymarket" and rows_of(view)
    assert all(r["market_id"] for r in rows_of(view))  # every row keeps the id the trade sheet needs


@pytest.mark.parametrize(("name", "layout"), KALSHI_LAYOUTS)
def test_kalshi_fixture_layout(name: str, layout: str) -> None:
    view = kalshi(name)
    assert view["layout"] == layout and view["volume_unit"] == "contracts" and rows_of(view)


@pytest.mark.parametrize(("name", "layout"), LIMITLESS_LAYOUTS)
def test_limitless_fixture_layout(name: str, layout: str) -> None:
    view = limitless(name)
    assert view["layout"] == layout and view["venue"] == "limitless" and len(rows_of(view)) == 1


def test_every_new_fixture_is_marked_synthetic_and_scrubbed() -> None:
    names = [("polymarket", n) for n, _ in POLYMARKET_LAYOUTS]
    names += [("kalshi", n) for n, _ in KALSHI_LAYOUTS] + [("kalshi", "live_data_batch")]
    names += [("limitless", n) for n, _ in LIMITLESS_LAYOUTS]
    for venue, name in names:
        text = (FIX / venue / f"{name}.json").read_text(encoding="utf-8")
        assert json.loads(text)["synthetic"] is True
        assert "ghp_" not in text and "Bearer" not in text


# ------------------------------------------------------------------------------------------------ tennis, MMA, esports
def test_tennis_players_are_rows_that_buy_their_own_outcome_and_score_follows_ordering() -> None:
    view = pm("tennis_match")
    game = view["game"]
    assert (game["home"]["name"], game["home"]["score"]) == ("Sinner", 1)
    assert (game["away"]["name"], game["away"]["score"]) == ("Alcaraz", 0)
    assert game["period"] == "S2" and game["live"] is True and view["status"] == "live"
    sections = {s["id"]: s for s in view["sections"]}
    players = [r for r in sections["game_lines"]["rows"] if r["label"] in {"Sinner", "Alcaraz"}]
    assert {r["label"]: r["yes"]["outcome"] for r in players} == {"Sinner": "YES", "Alcaraz": "NO"}
    chances = {r["label"]: Decimal(r["chance"]) for r in players}
    assert chances["Sinner"] + chances["Alcaraz"] == Decimal(1)  # the second row is the complement, not a new price


def test_mma_fight_without_a_score_is_scheduled_and_has_no_empty_tab() -> None:
    view = pm("mma_fight")
    assert view["status"] == "scheduled"
    assert view["game"]["home"]["score"] is None and view["game"]["away"]["score"] is None
    assert all(s["rows"] for s in view["sections"])  # never an empty tab
    assert "method_of_victory" in {s["id"] for s in view["sections"]}  # an unknown market type gets its own tab


def test_esports_unknown_market_type_gets_a_prettified_tab() -> None:
    view = pm("esports_match")
    labels = {s["id"]: s["label"] for s in view["sections"]}
    assert labels["child_moneyline"] == "Child Moneyline"
    assert view["game"]["period"] == "Map 2" and view["game"]["home"]["score"] == 1


# ------------------------------------------------------------------------------------------- mentions and weather
def test_mentions_are_candidates_sorted_by_chance_with_no_thresholds() -> None:
    view = pm("mentions")
    chances = [Decimal(r["chance"]) for r in rows_of(view)]
    assert chances == sorted(chances, reverse=True)
    assert [r["label"] for r in rows_of(view)] == ["Tariff", "Recession", "Layoffs"]


def test_weather_bands_are_a_bracket_in_ascending_order_even_when_shuffled() -> None:
    raw = load("polymarket", "weather_temperature")
    raw["markets"] = list(reversed(raw["markets"]))
    view = eventview.build_polymarket_view(raw, now=NOW)
    assert view["layout"] == "bracket_ladder"
    assert [r["label"] for r in rows_of(view)] == [
        "49\u00b0F or below",
        "50-51\u00b0F",
        "52-53\u00b0F",
        "54-55\u00b0F",
        "56\u00b0F or higher",
    ]
    assert view["chart"]["point_rule"] == "midpoint"


# ------------------------------------------------------------------------------------------ Kalshi and Limitless
def test_kalshi_sports_event_lists_both_sides_as_rows_with_the_ask_on_the_button() -> None:
    view = kalshi("sports_event")
    rows = {r["market_id"].rsplit("-", 1)[1]: r for r in rows_of(view)}
    assert set(rows) == {"LAL", "BOS"}
    assert rows["BOS"]["chance"] == "0.59" and rows["BOS"]["yes"]["ask"] == "0.60"  # midpoint shown, ask on the button
    assert view["category"]["path"] == ["Sports"]
    assert view.get("game") is None  # no score block until the milestone feed is linked (see WORKLOG P6)


def test_kalshi_binary_event_url_and_ticker() -> None:
    view = kalshi("binary_event")
    assert view["event_id"] == "KXRAIN-26NOV02" and view["url"] == "https://kalshi.com/markets/kxrain-26nov02"
    assert rows_of(view)[0]["market_id"] == "KXRAIN-26NOV02-Y"


def test_limitless_clob_and_recurring_markets_render_as_one_priced_row() -> None:
    clob, recurring = limitless("clob_market"), limitless("recurring_market")
    assert rows_of(clob)[0]["chance"] == "0.62" and rows_of(clob)[0]["no"]["ask"] == "0.38"
    assert rows_of(recurring)[0]["chance"] == "0.5"
    assert clob["category"]["path"] == ["Crypto"] and recurring["category"]["path"] == ["Crypto", "Hourly"]


# --------------------------------------------------------------------------------------------- robustness
@pytest.mark.parametrize(("name", "_layout"), POLYMARKET_LAYOUTS)
def test_stripped_polymarket_fixtures_still_render(name: str, _layout: str) -> None:
    raw = load("polymarket", name)
    for key in ("teams", "score", "period", "live", "startTime", "tags", "image"):
        stripped = copy.deepcopy(raw)
        stripped.pop(key, None)
        view = eventview.build_polymarket_view(stripped, now=NOW)
        assert view["layout"] in eventview.LAYOUTS and rows_of(view)


def test_fixture_directory_has_nothing_unaccounted_for() -> None:
    """A new capture must be added to a test above (or to the older tests), not just dropped into the folder."""

    known = {n for n, _ in POLYMARKET_LAYOUTS} | {
        "soccer_main",
        "soccer_more_markets",
        "soccer_halftime_result",
        "nba_game",
        "bracket_anthropic_ipo",
        "candidates",
        "crypto_updown_5m",
        "binary",
    }
    known |= {n for n, _ in KALSHI_LAYOUTS} | {"range_event", "live_data_batch"}
    known |= {n for n, _ in LIMITLESS_LAYOUTS} | {"group"}
    on_disk = {Path(p).stem for p in (FIX).glob("*/*.json")}
    assert on_disk <= known, sorted(on_disk - known)
