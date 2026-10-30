"""Contract tests over responses recorded from the live public APIs on 2026-10-30."""

from __future__ import annotations

import json
from pathlib import Path
from types import MethodType

from mm_agent.domain import MarketRef, MarketStatus, Outcome, VenueId
from mm_agent.venues.kalshi import KalshiVenue
from mm_agent.venues.limitless import LimitlessVenue
from mm_agent.venues.polymarket import PolymarketVenue
from tests.conftest import make_settings

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    # PowerShell's Set-Content utf8 writes a BOM; recorded raw bytes are preserved.
    return json.loads((FIXTURES / name).read_text(encoding="utf-8-sig"))


def test_gamma_fixture_parses_without_leaking_auth() -> None:
    venue = PolymarketVenue(make_settings(Path(".")))
    market = venue.parse_gamma_market(load("gamma_market.json"))
    assert market.venue is VenueId.POLYMARKET
    assert market.status is MarketStatus.OPEN
    assert len(market.outcomes) == 2
    assert market.outcome(Outcome.YES).native_id
    assert market.outcome(Outcome.NO).native_id
    assert market.tick_size > 0


def test_polymarket_book_fixture_sorted_by_adapter() -> None:
    venue = PolymarketVenue(make_settings(Path(".")))
    market = venue.parse_gamma_market(load("gamma_market.json"))
    venue._remember(market)
    raw_book = load("polymarket_book.json")

    async def fake_get_json(self, url, *, params=None, headers=None):  # type: ignore[no-untyped-def]
        return raw_book

    venue.get_json = MethodType(fake_get_json, venue)

    import asyncio

    book = asyncio.run(
        venue.get_book(MarketRef(venue=VenueId.POLYMARKET, market_id=market.native_id, outcome=Outcome.YES))
    )
    assert book.best_bid is not None and book.best_ask is not None
    assert book.best_bid < book.best_ask
    assert book.bids[0].price >= book.bids[-1].price
    assert book.asks[0].price <= book.asks[-1].price


def test_kalshi_event_fixture_produces_normal_market() -> None:
    venue = KalshiVenue(make_settings(Path(".")))
    event = load("kalshi_event.json")
    raw = dict(event["markets"][0])
    raw["event_title"] = event["title"]
    raw["category"] = event["category"]
    market = venue._parse_market(raw)
    assert market is not None
    assert market.venue is VenueId.KALSHI
    assert market.status is MarketStatus.OPEN
    assert market.tick_size > 0


def test_kalshi_book_fixture_only_bids_are_inverted() -> None:
    venue = KalshiVenue(make_settings(Path(".")))
    raw_book = load("kalshi_book.json")

    async def fake_get_json(self, url, *, params=None, headers=None):  # type: ignore[no-untyped-def]
        return raw_book

    venue.get_json = MethodType(fake_get_json, venue)
    import asyncio

    yes = asyncio.run(venue.get_book(MarketRef(venue=VenueId.KALSHI, market_id="KXELONMARS-99", outcome=Outcome.YES)))
    assert yes.best_bid is not None
    assert yes.best_ask is not None
    assert yes.best_bid < yes.best_ask


def test_limitless_market_and_book_fixtures() -> None:
    venue = LimitlessVenue(make_settings(Path(".")))
    market = venue.parse_market(load("limitless_market.json"))
    assert market.venue is VenueId.LIMITLESS
    assert market.outcome(Outcome.YES).native_id
    raw_book = load("limitless_book.json")

    async def fake_get_json(self, url, *, params=None, headers=None):  # type: ignore[no-untyped-def]
        return raw_book

    venue.get_json = MethodType(fake_get_json, venue)
    import asyncio

    ref = MarketRef(venue=VenueId.LIMITLESS, market_id=market.native_id, outcome=Outcome.NO)
    book = asyncio.run(venue.get_book(ref))
    for level in (*book.bids, *book.asks):
        assert 0 < level.price < 1


def test_fixture_files_are_present() -> None:
    for name in [
        "gamma_market.json",
        "polymarket_book.json",
        "kalshi_event.json",
        "kalshi_book.json",
        "limitless_market.json",
        "limitless_book.json",
    ]:
        assert (FIXTURES / name).exists()
