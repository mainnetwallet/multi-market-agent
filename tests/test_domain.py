from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from mm_agent.domain import (
    BookLevel,
    BookSnapshot,
    Outcome,
    UnitMismatchError,
    VenueId,
    normalize_probability,
    quantize_price,
    utcnow,
)


def test_probability_normalizer_rejects_cents_and_percentages() -> None:
    assert normalize_probability("0.42") == Decimal("0.42")
    for bad in ["42", "42c", "1.0", "0", "-0.1", "1.2"]:
        with pytest.raises(UnitMismatchError):
            normalize_probability(bad)


def test_tick_rounding_never_overpays() -> None:
    assert quantize_price(Decimal("0.434"), Decimal("0.01")) == Decimal("0.43")
    assert quantize_price(Decimal("0.430"), Decimal("0.001")) == Decimal("0.430")


def test_book_walk_and_synthetic_flag() -> None:
    book = BookSnapshot(
        venue=VenueId.FAKE,
        market_id="m",
        outcome=Outcome.YES,
        asks=(
            BookLevel(price=Decimal("0.40"), size=Decimal("10")),
            BookLevel(price=Decimal("0.42"), size=Decimal("10")),
        ),
    )
    assert book.walk(Decimal("15")) == (Decimal("4.0") + Decimal("2.1")) / Decimal("15")
    assert book.walk(Decimal("21")) is None
    thin = book.model_copy(
        update={
            "asks": (
                BookLevel(price=Decimal("0.4"), size=Decimal("50000")),
                BookLevel(price=Decimal("0.5"), size=Decimal("1")),
            )
        }
    )
    assert thin.synthetic_flag()


def test_old_book_is_not_fresh() -> None:
    book = BookSnapshot(
        venue=VenueId.FAKE,
        market_id="m",
        outcome=Outcome.YES,
        asks=(BookLevel(price=Decimal("0.4"), size=Decimal("1")),),
        timestamp=utcnow() - timedelta(minutes=5),
    )
    assert book.age_seconds() > 30
