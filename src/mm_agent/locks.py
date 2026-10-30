"""Lock registry: one live action per market, one placement per venue."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from .domain import VenueId


class LockRegistry:
    def __init__(self) -> None:
        self._market_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._venue_locks: dict[VenueId, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._registry_lock = asyncio.Lock()

    async def market(self, namespaced_market_ref: str) -> asyncio.Lock:
        async with self._registry_lock:
            return self._market_locks[namespaced_market_ref]

    async def venue(self, venue: VenueId) -> asyncio.Lock:
        async with self._registry_lock:
            return self._venue_locks[venue]
