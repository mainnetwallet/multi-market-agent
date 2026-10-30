"""Book cache, depth analysis and the startup unit guard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .domain import (
    BookSnapshot,
    Market,
    MarketRef,
    Outcome,
    UnitMismatchError,
    normalize_probability,
    utcnow,
)
from .util import get_logger
from .venues.base import Venue

logger = get_logger(__name__)


@dataclass(frozen=True)
class BookAnalysis:
    outcome: Outcome
    mid: Decimal | None
    spread: Decimal | None
    best_ask: Decimal | None
    top_depth_shares: Decimal
    depth_for_size: Decimal
    vwap: Decimal | None
    synthetic: bool
    age_seconds: float

    @property
    def tradable(self) -> bool:
        return self.mid is not None and self.spread is not None and self.best_ask is not None and not self.synthetic


class BookCache:
    def __init__(self, fresh_seconds: int = 30) -> None:
        self.fresh_seconds = fresh_seconds
        self._books: dict[str, BookSnapshot] = {}

    def put(self, book: BookSnapshot) -> None:
        self._books[book.key] = book

    def get_cached(self, ref: MarketRef, max_age: int | None = None) -> BookSnapshot | None:
        book = self._books.get(ref.namespaced)
        if book is None:
            return None
        limit = max_age if max_age is not None else self.fresh_seconds
        if book.age_seconds() > limit:
            return None
        return book

    async def get(
        self, venue: Venue, ref: MarketRef, *, force: bool = False, max_age: int | None = None
    ) -> BookSnapshot:
        if not force:
            cached = self.get_cached(ref, max_age)
            if cached is not None:
                return cached
        book = await venue.get_book(ref)
        self.put(book)
        return book

    @staticmethod
    def analyse(book: BookSnapshot, shares: Decimal) -> BookAnalysis:
        return BookAnalysis(
            outcome=book.outcome,
            mid=book.mid,
            spread=book.spread,
            best_ask=book.best_ask,
            top_depth_shares=book.asks[0].size if book.asks else Decimal("0"),
            depth_for_size=book.depth_shares(),
            vwap=book.walk(shares) if shares > 0 else None,
            synthetic=book.synthetic_flag(),
            age_seconds=book.age_seconds(),
        )


class UnitGuard:
    """One normalization function, checked at startup.

    A mismatch halts new trading globally. The agent keeps monitoring exits.
    """

    def __init__(self) -> None:
        self.ok = False
        self.reason = "not checked"
        self.checked_at: datetime | None = None

    def check_math(self) -> None:
        if normalize_probability("0.37") != Decimal("0.37"):
            raise UnitMismatchError("probability normalizer returned the wrong value")
        for bad in ("37", "37c", "1.0", "0", "-0.2", "1.02"):
            try:
                normalize_probability(bad)
            except UnitMismatchError:
                continue
            raise UnitMismatchError(f"probability normalizer accepted invalid value {bad!r}")

    async def run(self, venue: Venue, market: Market) -> None:
        if not market.outcomes:
            raise UnitMismatchError("market has no outcomes")
        for info in market.outcomes:
            ref = MarketRef(venue=market.venue, market_id=market.native_id, outcome=info.outcome)
            book = await venue.get_book(ref)
            for level in (*book.bids, *book.asks):
                if not (Decimal("0") < level.price < Decimal("1")):
                    raise UnitMismatchError(f"{book.key} level outside probability range")
            if book.is_crossed():
                raise UnitMismatchError(f"{book.key} book is crossed")
        self.ok = True
        self.reason = ""
        self.checked_at = utcnow()
        logger.info("unit_guard_ok", venue=venue.display_name, market=market.native_id)

    def halt(self, reason: str) -> None:
        self.ok = False
        self.reason = reason
        logger.error("unit_guard_halt", reason=reason)
