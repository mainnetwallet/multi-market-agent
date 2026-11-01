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


@pytest.mark.parametrize(
    ("raw", "event", "expected"),
    [
        ({"category": "Crypto"}, {}, "Crypto"),
        ({"category": ""}, {"category": "Sports"}, "Sports"),
        ({}, {"tags": [{"label": "NBA", "slug": "nba"}, {"label": "Sports", "slug": "sports"}]}, "Sports"),
        ({"categories": [{"label": "Politics", "slug": "politics"}]}, {}, "Politics"),
        ({"tags": ["crypto", "bitcoin"]}, {}, "Crypto"),
        # Several schedule names: the highest taker rate wins, so a fee is never understated.
        ({"category": "Politics", "tags": [{"slug": "crypto"}]}, {}, "Crypto"),
        # A name outside the schedule is kept; nothing at all becomes "Other".
        ({"category": "US-current-affairs"}, {}, "US-current-affairs"),
        ({}, {}, "Other"),
        ({"tags": [{"label": "Trump"}], "category": None}, {"tags": None}, "Other"),
    ],
)
def test_polymarket_category_resolution(raw: dict, event: dict, expected: str) -> None:
    from mm_agent.venues.polymarket import resolve_category

    assert resolve_category(raw, event) == expected


def test_polymarket_fee_rate_follows_resolved_category() -> None:
    venue = PolymarketVenue(make_settings(__import__("pathlib").Path(".")))
    market = venue.parse_gamma_market(
        {
            "id": "55",
            "question": "BTC up?",
            "clobTokenIds": '["1", "2"]',
            "outcomes": '["Yes", "No"]',
            "active": True,
            "closed": False,
            "acceptingOrders": True,
            "events": [{"id": "e", "tags": [{"label": "Crypto", "slug": "crypto"}]}],
        }
    )
    assert market.category == "Crypto"
    assert market.fee_info["taker_rate"] == "0.07"


# A Gamma row shaped like the live one: no category or tags anywhere, a zero feeSchedule.
GAMMA_ROW_NO_CATEGORY = {
    "id": "559651",
    "question": "Xi Jinping out before 2027?",
    "clobTokenIds": '["111", "222"]',
    "outcomes": '["Yes", "No"]',
    "active": True,
    "closed": False,
    "acceptingOrders": True,
    "feesEnabled": True,
    "feeSchedule": {"exponent": 1, "rate": 0, "takerOnly": True, "rebateRate": 0},
    "events": [{"id": "30828", "title": "Xi Jinping out before 2027?", "slug": "xi"}],
}


def _polymarket_with_tags(tags: object) -> tuple[PolymarketVenue, list[str]]:
    venue = PolymarketVenue(make_settings(__import__("pathlib").Path(".")))
    calls: list[str] = []

    async def fake_get_json(self, url, *, params=None, headers=None):  # type: ignore[no-untyped-def]
        calls.append(url)
        if isinstance(tags, Exception):
            raise tags
        return tags

    venue.get_json = MethodType(fake_get_json, venue)
    return venue, calls


async def test_polymarket_category_from_event_tags() -> None:
    venue, calls = _polymarket_with_tags(
        [{"label": "Politics", "slug": "politics"}, {"label": "China", "slug": "china"}]
    )
    market = venue.parse_gamma_market(GAMMA_ROW_NO_CATEGORY)
    assert market.category == "Other"
    assert market.fee_info["category_resolved"] is False
    (market,) = await venue.with_event_categories([market])
    assert calls == ["https://gamma-api.polymarket.com/events/30828/tags"]
    assert market.category == "Politics"
    # The market's own feeSchedule says 0, but the rate is never taken below the category's.
    assert market.fee_info["taker_rate"] == "0.04"
    assert market.fee_info["category_resolved"] is True


async def test_polymarket_event_tags_are_fetched_once_per_event() -> None:
    venue, calls = _polymarket_with_tags([{"label": "Crypto", "slug": "crypto"}])
    first = venue.parse_gamma_market(GAMMA_ROW_NO_CATEGORY)
    second = venue.parse_gamma_market({**GAMMA_ROW_NO_CATEGORY, "id": "559652"})
    out = await venue.with_event_categories([first, second])
    out += await venue.with_event_categories([first])
    assert len(calls) == 1
    assert [m.fee_info["taker_rate"] for m in out] == ["0.07", "0.07", "0.07"]


async def test_polymarket_failed_tag_lookup_assumes_the_highest_rate() -> None:
    venue, calls = _polymarket_with_tags(RuntimeError("gamma down"))
    market = venue.parse_gamma_market(GAMMA_ROW_NO_CATEGORY)
    (market,) = await venue.with_event_categories([market])
    assert market.fee_info["taker_rate"] == "0.07"
    assert market.fee_info["category_lookup"] == "failed"
    assert market.fee_info["category_resolved"] is False
    # The failure is remembered briefly, so a second pass does not call Gamma again.
    await venue.with_event_categories([market])
    assert len(calls) == 1


async def test_polymarket_unmatched_tags_resolve_to_other_at_five_percent() -> None:
    venue, _ = _polymarket_with_tags([{"label": "Trump", "slug": "trump"}])
    (market,) = await venue.with_event_categories([venue.parse_gamma_market(GAMMA_ROW_NO_CATEGORY)])
    assert market.category == "Other"
    assert market.fee_info["taker_rate"] == "0.05"
    assert market.fee_info["category_resolved"] is True


async def test_polymarket_market_schedule_rate_above_category_wins() -> None:
    venue, _ = _polymarket_with_tags([{"label": "Politics", "slug": "politics"}])
    row = {**GAMMA_ROW_NO_CATEGORY, "feeSchedule": {"rate": 0.07, "exponent": 1}}
    (market,) = await venue.with_event_categories([venue.parse_gamma_market(row)])
    assert market.fee_info["taker_rate"] == "0.07"


async def test_polymarket_get_market_fills_category_of_a_remembered_card() -> None:
    from mm_agent.domain import MarketRef

    venue, calls = _polymarket_with_tags([{"label": "Sports", "slug": "sports"}])
    venue._remember(venue.parse_gamma_market(GAMMA_ROW_NO_CATEGORY))
    market = await venue.get_market(MarketRef(venue=VenueId.POLYMARKET, market_id="559651"))
    assert market.category == "Sports"
    assert venue._markets["559651"].category == "Sports"
    assert len(calls) == 1


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


async def test_polymarket_price_history_uses_market_param_and_fidelity() -> None:
    venue = PolymarketVenue(make_settings(__import__("pathlib").Path(".")))
    seen: list[tuple[str, dict]] = []

    async def fake_get_market(self, ref):  # type: ignore[no-untyped-def]
        return venue.parse_gamma_market(GAMMA_ROW_NO_CATEGORY)

    async def fake_get_json(self, url, *, params=None, headers=None):  # type: ignore[no-untyped-def]
        seen.append((url, dict(params or {})))
        return {"history": [{"t": 1767225600, "p": 0.41}, {"t": 1767229200, "p": 0.43}, {"bad": 1}]}

    venue.get_market = MethodType(fake_get_market, venue)
    venue.get_json = MethodType(fake_get_json, venue)
    points = await venue.get_price_history("30828", interval="1w")
    url, params = seen[0]
    assert url == "https://clob.polymarket.com/prices-history"
    # `token_id` is rejected by the CLOB with HTTP 400; the parameter is `market`.
    assert params == {"market": "111", "interval": "1w", "fidelity": 60}
    assert [p["close"] for p in points] == ["0.41", "0.43"]
    with pytest.raises(ValueError):
        await venue.get_price_history("30828", interval="5y")
