from __future__ import annotations

from decimal import Decimal

import pytest

from mm_agent.domain import (
    BookLevel,
    BookSnapshot,
    Market,
    MarketStatus,
    Outcome,
    OutcomeInfo,
    Side,
    VenueId,
)
from mm_agent.risk import RiskEngine, RiskSnapshot
from mm_agent.strategy import Signal
from tests.conftest import make_settings


def make_market() -> Market:
    return Market(
        venue=VenueId.FAKE,
        native_id="m",
        title="T",
        status=MarketStatus.OPEN,
        outcomes=(
            OutcomeInfo(outcome=Outcome.YES, native_id="y", label="Yes"),
            OutcomeInfo(outcome=Outcome.NO, native_id="n", label="No"),
        ),
    )


def make_signal(market: Market) -> Signal:
    book = BookSnapshot(
        venue=VenueId.FAKE,
        market_id="m",
        outcome=Outcome.YES,
        bids=(BookLevel(price=Decimal("0.44"), size=Decimal("1000")),),
        asks=(BookLevel(price=Decimal("0.45"), size=Decimal("1000")),),
    )
    return Signal(
        venue=VenueId.FAKE,
        market=market,
        outcome=Outcome.YES,
        side=Side.BUY,
        book=book,
        price=Decimal("0.45"),
        vwap=Decimal("0.45"),
        fee_per_share=Decimal("0.01"),
        edge=Decimal("0.12"),
        p_fair=Decimal("0.58"),
        p_side=Decimal("0.58"),
        max_shares=Decimal("4"),
        spread=Decimal("0.01"),
        depth_multiple=Decimal("100"),
        min_order_shares=Decimal("1"),
        min_order_notional=Decimal("1"),
    )


def test_kelly_and_hard_caps(tmp_path) -> None:
    settings = make_settings(tmp_path)
    engine = RiskEngine(settings)
    market = make_market()
    signal = make_signal(market)
    snapshot = RiskSnapshot(
        bankroll_usd=Decimal("100"),
        open_positions=0,
        event_exposure_usd=Decimal("0"),
        total_exposure_usd=Decimal("0"),
        daily_pnl_usd=Decimal("0"),
    )
    decision = engine.size(signal, snapshot, venue=VenueId.FAKE)
    assert decision.approved
    assert decision.size_usd <= Decimal("2")
    assert decision.binding_cap == "MAX_TRADE"


def test_daily_loss_veto(tmp_path) -> None:
    settings = make_settings(tmp_path)
    engine = RiskEngine(settings)
    market = make_market()
    snapshot = RiskSnapshot(
        bankroll_usd=Decimal("100"),
        open_positions=0,
        event_exposure_usd=Decimal("0"),
        total_exposure_usd=Decimal("0"),
        daily_pnl_usd=Decimal("-5"),
    )
    decision = engine.size(
        make_signal(market),
        snapshot,
        venue=VenueId.FAKE,
    )
    assert not decision.approved
    assert "DAILY_LOSS_CAP" in decision.vetoes


def test_runtime_caps_can_only_lower(tmp_path) -> None:
    settings = make_settings(tmp_path)
    engine = RiskEngine(settings)
    assert engine.set_cap("max_trade_usd", Decimal("1")) == Decimal("1")
    with pytest.raises(ValueError):
        engine.set_cap("max_trade_usd", Decimal("2"))
    assert engine.set_cap("max_open_positions", Decimal("3")) == Decimal("3")


def test_per_venue_bankroll_override(tmp_path) -> None:
    settings = make_settings(tmp_path, bankroll_usd=Decimal("100"), bankroll_polymarket=Decimal("25"))
    assert settings.limit_bankroll(VenueId.POLYMARKET) == Decimal("25")
    assert settings.limit_bankroll(VenueId.KALSHI) == Decimal("100")
