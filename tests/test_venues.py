from __future__ import annotations

from decimal import Decimal
from types import MethodType

import pytest

from mm_agent.domain import MarketStatus, Outcome, VenueId
from mm_agent.venues.kalshi import KalshiVenue
from mm_agent.venues.limitless import LimitlessVenue
from mm_agent.venues.polymarket import PolymarketVenue
from tests.conftest import make_settings


def test_polymarket_gamma_parser() -> None:
    venue = PolymarketVenue(make_settings(__import__("pathlib").Path(".")))
    market = venue.parse_gamma_market(
        {
            "id": "123",
            "question": "Will it rain?",
            "conditionId": "0xcondition",
            "clobTokenIds": '["111", "222"]',
            "outcomes": '["Yes", "No"]',
            "active": True,
            "closed": False,
            "acceptingOrders": True,
            "orderPriceMinTickSize": 0.001,
            "orderMinSize": 5,
            "endDate": "2026-12-01T00:00:00Z",
            "events": [{"id": "e1", "title": "Weather", "slug": "weather", "category": "Weather"}],
        }
    )
    assert market.venue is VenueId.POLYMARKET
    assert market.outcome(Outcome.YES).native_id == "111"
    assert market.outcome(Outcome.NO).native_id == "222"
    assert market.tick_size == Decimal("0.001")
    assert market.status is MarketStatus.OPEN
    assert market.fee_info["taker_rate"] == "0.05"


def test_polymarket_v2_position_ids() -> None:
    venue = PolymarketVenue(make_settings(__import__("pathlib").Path(".")))
    market = venue.parse_gamma_market(
        {
            "id": "9",
            "question": "V2?",
            "positionIds": ["9001", "9002"],
            "clobTokenIds": None,
            "outcomes": '["Yes", "No"]',
            "active": True,
            "closed": False,
            "acceptingOrders": True,
        }
    )
    assert market.outcome(Outcome.YES).native_id == "9001"


def test_kalshi_parser_skips_multivariate_and_reads_fixed_point() -> None:
    venue = KalshiVenue(make_settings(__import__("pathlib").Path(".")))
    base = {
        "ticker": "KXTEST-26DEC31",
        "event_ticker": "KXTEST",
        "title": "Will the test pass?",
        "status": "active",
        "close_time": "2026-12-31T00:00:00Z",
        "price_ranges": [
            {"start": "0.0000", "end": "0.0100", "step": "0.0001"},
            {"start": "0.0100", "end": "0.9900", "step": "0.0010"},
            {"start": "0.9900", "end": "1.0000", "step": "0.0001"},
        ],
        "market_type": "binary",
    }
    market = venue._parse_market(base)
    assert market is not None
    assert market.tick_size == Decimal("0.0010")
    assert market.status is MarketStatus.OPEN
    assert market.fee_info["taker_rate"] == "0.07"
    assert venue._parse_market({**base, "mve_collection_ticker": "KXMVE"}) is None


def test_kalshi_exact_tick_band_is_used_at_edges() -> None:
    venue = KalshiVenue(make_settings(__import__("pathlib").Path(".")))
    base = {
        "ticker": "KXTEST-EDGE",
        "event_ticker": "KXTEST",
        "title": "Edge?",
        "status": "active",
        "market_type": "binary",
        "price_ranges": [
            {"start": "0.0000", "end": "0.0100", "step": "0.0001"},
            {"start": "0.0100", "end": "0.9900", "step": "0.0010"},
            {"start": "0.9900", "end": "1.0000", "step": "0.0001"},
        ],
    }
    market = venue._parse_market(base)
    assert market is not None
    assert venue._tick_for_price(market, Decimal("0.995")) == Decimal("0.0001")
    assert venue._tick_for_price(market, Decimal("0.500")) == Decimal("0.001")


@pytest.mark.asyncio
async def test_kalshi_book_inverts_no_bids() -> None:
    venue = KalshiVenue(make_settings(__import__("pathlib").Path(".")))

    async def fake_get_json(self, url, *, params=None, headers=None):  # type: ignore[no-untyped-def]
        return {"orderbook_fp": {"yes_dollars": [["0.42", "100"]], "no_dollars": [["0.55", "50"]]}}

    venue.get_json = MethodType(fake_get_json, venue)

    from mm_agent.domain import MarketRef

    yes = await venue.get_book(MarketRef(venue=VenueId.KALSHI, market_id="M", outcome=Outcome.YES))
    assert yes.best_bid == Decimal("0.42")
    assert yes.best_ask == Decimal("0.45")
    no = await venue.get_book(MarketRef(venue=VenueId.KALSHI, market_id="M", outcome=Outcome.NO))
    assert no.best_bid == Decimal("0.55")
    assert no.best_ask == Decimal("0.58")


def test_limitless_parser_reads_tokens_and_fee() -> None:
    venue = LimitlessVenue(make_settings(__import__("pathlib").Path(".")))
    market = venue.parse_market(
        {
            "slug": "test-market",
            "title": "Test?",
            "status": "FUNDED",
            "tradeType": "clob",
            "tokens": {"yes": "y", "no": "n"},
            "categories": ["Crypto"],
            "expirationTimestamp": 1793346900000,
            "metadata": {"fee": True},
            "venue": {"exchange": "0x" + "2" * 40, "feeModuleType": "COLLATERAL"},
        }
    )
    assert market.outcome(Outcome.YES).native_id == "y"
    assert market.fee_info["fee_enabled"] is True
    assert market.status is MarketStatus.OPEN


@pytest.mark.asyncio
async def test_limitless_book_derives_no_side() -> None:
    venue = LimitlessVenue(make_settings(__import__("pathlib").Path(".")))

    async def fake_get_json(self, url, *, params=None, headers=None):  # type: ignore[no-untyped-def]
        return {
            "bids": [{"price": 0.40, "size": 2_000_000, "side": "BUY"}],
            "asks": [{"price": 0.44, "size": 3_000_000, "side": "SELL"}],
        }

    venue.get_json = MethodType(fake_get_json, venue)
    from mm_agent.domain import MarketRef

    no = await venue.get_book(MarketRef(venue=VenueId.LIMITLESS, market_id="slug", outcome=Outcome.NO))
    assert no.best_bid == Decimal("0.56")
    assert no.best_ask == Decimal("0.60")
    assert no.asks[0].size == Decimal("2")


def test_production_registry_has_no_simulated_venue(tmp_path) -> None:
    """Every order is real: the app must not be able to enable or switch to a test venue."""

    import pytest

    from mm_agent.events import EventBus
    from mm_agent.store import Store
    from mm_agent.venues.registry import VenueRegistry
    from tests.conftest import make_settings

    settings = make_settings(tmp_path)  # enabled_venues_raw="fake"
    store = Store(settings.database_url)
    store.create_all()
    with pytest.raises(ValueError, match="unknown venue"):
        VenueRegistry(settings, store, EventBus())

    real = make_settings(tmp_path, enabled_venues_raw="polymarket,kalshi,limitless", active_venue="polymarket")
    registry = VenueRegistry(real, store, EventBus())
    assert set(registry.adapters) == {"polymarket", "kalshi", "limitless"}
    assert registry.is_ready(VenueId.FAKE) is False
    store.close()
