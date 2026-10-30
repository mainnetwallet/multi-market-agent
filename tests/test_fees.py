from __future__ import annotations

from decimal import Decimal

from mm_agent.domain import Market, MarketStatus, Outcome, OutcomeInfo, Side, VenueId
from mm_agent.venues.fake import FakeVenue
from mm_agent.venues.kalshi import KalshiVenue
from tests.conftest import make_settings


def market() -> Market:
    return Market(
        venue=VenueId.KALSHI,
        native_id="T",
        title="T",
        status=MarketStatus.OPEN,
        outcomes=(
            OutcomeInfo(outcome=Outcome.YES, native_id="T", label="Yes"),
            OutcomeInfo(outcome=Outcome.NO, native_id="T", label="No"),
        ),
    )


def test_kalshi_fee_rounds_up_to_cent(tmp_path) -> None:
    venue = KalshiVenue(make_settings(tmp_path))
    quote = venue.fee(Decimal("0.50"), Decimal("3"), Side.BUY, market())
    # 0.07 * 3 * 0.5 * 0.5 = 0.0525 -> ceil to 0.06
    assert quote.fee_usd == Decimal("0.06")
    sell = venue.fee(Decimal("0.50"), Decimal("3"), Side.SELL, market())
    assert sell.fee_usd == Decimal("0.06")  # marketable exits are taker too


def test_fake_fee_is_charged_both_sides(tmp_path) -> None:
    venue = FakeVenue(make_settings(tmp_path))
    m = market().model_copy(update={"venue": VenueId.FAKE, "fee_info": {"taker_rate": "0.05"}})
    assert venue.fee(Decimal("0.5"), Decimal("10"), Side.BUY, m).fee_usd == Decimal("0.125")
    assert venue.fee(Decimal("0.5"), Decimal("10"), Side.SELL, m).fee_usd == Decimal("0.125")
