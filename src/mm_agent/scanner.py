"""Market scanner. One venue- and category-agnostic loop."""

from __future__ import annotations

import asyncio
from collections import Counter

from .config import Settings
from .domain import FeedDiag, FeedVenueDiag, MarketStatus, VenueId, utcnow
from .events import EventBus
from .store import Store
from .util import get_logger
from .venues.base import MarketQuery
from .venues.registry import VenueRegistry

logger = get_logger(__name__)


class Scanner:
    def __init__(self, settings: Settings, store: Store, registry: VenueRegistry, bus: EventBus) -> None:
        self.settings = settings
        self.store = store
        self.registry = registry
        self.bus = bus
        self.diag: dict[VenueId, FeedVenueDiag] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.last_error: str | None = None

    def feed_diag(self) -> FeedDiag:
        return FeedDiag(venues={venue.value: diag for venue, diag in self.diag.items()})

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self.run_forever(), name="scanner")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.scan_once()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("scan_loop_error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.scan_interval_seconds)
            except TimeoutError:
                continue

    async def scan_once(self) -> FeedDiag:
        if self.settings.parallel_scan:
            venues = self.registry.enabled()
        else:
            active = self.registry.active_or_none
            venues = (active,) if active is not None else ()
        sem = asyncio.Semaphore(3)

        async def scan(venue: VenueId) -> None:
            async with sem:
                await self._scan_venue(venue)

        await asyncio.gather(*(scan(v) for v in venues))
        await self.bus.publish(
            "scanner.tick",
            feed_diag=self.feed_diag().model_dump(mode="json"),
            markets=self.store.market_count(),
        )
        return self.feed_diag()

    async def _scan_venue(self, venue: VenueId) -> None:
        diag = self.diag.setdefault(venue, FeedVenueDiag())
        diag.last_run = utcnow()
        adapter = self.registry.get(venue)
        if not self.registry.is_ready(venue):
            diag.error = self.registry.reason(venue) or "venue not ready"
            return
        try:
            page = await adapter.list_markets(MarketQuery(limit=self.settings.markets_per_scan, include_closed=False))
            diag.pages += 1
            diag.seen += len(page.items)
            kept = 0
            families: Counter[str] = Counter()
            seen_ids: set[str] = set()
            for market in page.items:
                if market.ref in seen_ids:
                    diag.dropped_duplicate += 1
                    continue
                seen_ids.add(market.ref)
                if market.status is not MarketStatus.OPEN:
                    diag.dropped_closed += 1
                    continue
                if len(market.outcomes) < 2:
                    diag.dropped_no_tokens += 1
                    continue
                if not all(
                    market.outcome(o).native_id for o in (market.outcomes[0].outcome, market.outcomes[1].outcome)
                ):
                    diag.dropped_no_tokens += 1
                    continue
                if market.tick_size <= 0 or market.tick_size >= 1:
                    diag.dropped_parse += 1
                    continue
                self.store.upsert_market(market)
                families[market.category or "Other"] += 1
                kept += 1
            diag.kept = kept
            diag.families = dict(families)
            diag.error = None
            diag.last_ok = utcnow()
            logger.info(
                "scan_venue",
                venue=venue.value,
                seen=len(page.items),
                kept=kept,
                families=dict(families),
            )
        except Exception as exc:
            diag.error = f"{type(exc).__name__}: {str(exc)[:240]}"
            logger.warning("scan_venue_failed", venue=venue.value, error=diag.error)
