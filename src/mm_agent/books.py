"""Book cache, depth analysis and the startup unit guard."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .domain import (
    BookSnapshot,
    Market,
    MarketRef,
    MarketStatus,
    Outcome,
    UnitMismatchError,
    normalize_probability,
    utcnow,
)
from .util import get_logger
from .venues.base import Venue

if TYPE_CHECKING:
    from .config import Settings
    from .events import EventBus
    from .store import Store
    from .venues.registry import VenueRegistry

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


@dataclass
class BookFeed:
    """Background book refresher for the active venue's tradable candidates.

    Adapters may later replace polling with native WebSockets without changing
    the cache or the pipeline. Feed freshness/errors are exposed in /status.
    """

    settings: Settings
    registry: VenueRegistry
    store: Store
    cache: BookCache
    bus: EventBus
    updates: int = 0
    errors: int = 0
    last_error: str | None = None
    last_update: datetime | None = None
    venues: dict[str, int] = field(default_factory=dict)
    _task: asyncio.Task[None] | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._task and not self._task.done()),
            "updates": self.updates,
            "errors": self.errors,
            "last_error": self.last_error,
            "last_update": self.last_update.isoformat() if self.last_update else None,
            "venues": dict(self.venues),
        }

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._loop(), name="book-feed")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.book_feed_interval_seconds)
            except TimeoutError:
                continue

    async def run_once(self) -> int:
        venue = self.registry.active_or_none
        if venue is None or not self.registry.is_ready(venue):
            return 0
        adapter = self.registry.get(venue)
        markets = self.store.list_markets(venue=venue, status=MarketStatus.OPEN, limit=self.settings.book_feed_markets)
        refs = [
            MarketRef(venue=venue, market_id=market.native_id, outcome=outcome)
            for market in markets
            for outcome in (Outcome.YES, Outcome.NO)
        ]
        if not refs:
            return 0

        async def fetch(ref: MarketRef) -> BookSnapshot:
            return await asyncio.wait_for(adapter.get_book(ref), timeout=12)

        results = await asyncio.gather(*(fetch(ref) for ref in refs), return_exceptions=True)
        count = 0
        for result in results:
            if isinstance(result, BaseException):
                self.errors += 1
                self.last_error = f"{type(result).__name__}: {str(result)[:160]}"
                continue
            self.cache.put(result)
            count += 1
        self.updates += count
        self.venues[venue.value] = self.venues.get(venue.value, 0) + count
        if count:
            self.last_update = utcnow()
        await self.bus.publish("books.refreshed", venue=venue.value, count=count, errors=self.errors)
        return count


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
