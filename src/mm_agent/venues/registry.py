"""Venue registry and runtime switching."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..domain import DoctorReport, DoctorStatus, MarketRef, Mode, VenueId, utcnow
from ..events import EventBus
from ..store import Store
from ..util import get_logger
from .base import Venue
from .fake import FakeVenue
from .kalshi import KalshiVenue
from .limitless import LimitlessVenue
from .polymarket import PolymarketVenue

logger = get_logger(__name__)


@dataclass
class VenueStatus:
    venue: VenueId
    display_name: str
    enabled: bool
    ready: bool
    reason: str
    live: bool
    mode: Mode
    doctor: DoctorReport | None = None


class VenueRegistry:
    def __init__(self, settings: Settings, store: Store, bus: EventBus) -> None:
        self.settings = settings
        self.store = store
        self.bus = bus
        self.adapters: dict[VenueId, Venue] = {
            VenueId.POLYMARKET: PolymarketVenue(settings),
            VenueId.KALSHI: KalshiVenue(settings),
            VenueId.LIMITLESS: LimitlessVenue(settings),
            VenueId.FAKE: FakeVenue(settings),
        }
        self.doctors: dict[VenueId, DoctorReport] = {}
        self._ready: dict[VenueId, bool] = {}
        self._reasons: dict[VenueId, str] = {}
        self._active: VenueId | None = None
        self._switch_lock = asyncio.Lock()

    async def start(self) -> None:
        for adapter in self.adapters.values():
            await adapter.start()

    async def close(self) -> None:
        for adapter in self.adapters.values():
            await adapter.close()

    @property
    def active(self) -> VenueId:
        if self._active is None:
            raise RuntimeError("no ready venue is active")
        return self._active

    @property
    def active_or_none(self) -> VenueId | None:
        return self._active

    def enabled(self) -> tuple[VenueId, ...]:
        return self.settings.enabled_venues

    def get(self, venue: VenueId) -> Venue:
        return self.adapters[venue]

    def for_active(self) -> Venue:
        return self.adapters[self.active]

    def is_ready(self, venue: VenueId) -> bool:
        if venue is VenueId.FAKE:
            return True
        return bool(self._ready.get(venue))

    def mode(self, venue: VenueId) -> Mode:
        return Mode.LIVE if self.settings.live_for(venue) else Mode.PAPER

    def reason(self, venue: VenueId) -> str:
        if not self._ready.get(venue):
            return self._reasons.get(venue, "doctor not run")
        if self.settings.live_for(venue):
            doctor = self.doctors.get(venue)
            if doctor and doctor.status is DoctorStatus.OK:
                return ""
            return "LIVE requested but authenticated doctor checks failed"
        return ""

    def statuses(self) -> list[VenueStatus]:
        return [
            VenueStatus(
                venue=venue,
                display_name=self.adapters[venue].display_name,
                enabled=True,
                ready=self.is_ready(venue),
                reason=self.reason(venue),
                live=self.settings.live_for(venue),
                mode=self.mode(venue),
                doctor=self.doctors.get(venue),
            )
            for venue in self.settings.enabled_venues
        ]

    async def refresh_all(self, *, test_orders: bool = False) -> dict[VenueId, DoctorReport]:
        for venue in self.enabled():
            adapter = self.adapters[venue]
            if hasattr(adapter, "test_orders"):
                adapter.test_orders = test_orders
            try:
                report = await adapter.doctor()
            except Exception as exc:
                report = DoctorReport(
                    venue=venue,
                    status=DoctorStatus.UNAVAILABLE,
                    reason=f"{type(exc).__name__}: {str(exc)[:200]}",
                )
            self._ingest(report)
        await self._choose_active(initial=True)
        await self.bus.publish(
            "venues.refreshed",
            venues=[
                {"venue": s.venue.value, "ready": s.ready, "live": s.live, "reason": s.reason} for s in self.statuses()
            ],
            active=self._active.value if self._active else None,
        )
        return dict(self.doctors)

    def _ingest(self, report: DoctorReport) -> None:
        venue = report.venue
        self.doctors[venue] = report
        public_checks = [c for c in report.checks if c.name in {"public_read", "market_read", "book_read"}]
        public_ok = bool(public_checks) and all(c.status is not DoctorStatus.UNAVAILABLE for c in public_checks)
        auth_checks = [c for c in report.checks if c.name == "authenticated_read"]
        auth_ok = any(c.status is DoctorStatus.OK for c in auth_checks)
        ready = public_ok
        reason = report.reason
        if self.settings.live_for(venue) and not auth_ok:
            ready = False
            reason = "LIVE requires passing authenticated doctor checks"
        if report.status is DoctorStatus.UNAVAILABLE:
            ready = False
            reason = report.reason or "doctor unavailable"
        self._ready[venue] = ready
        self._reasons[venue] = reason
        self.store.set_venue_state(
            venue,
            ready=ready,
            reason=reason,
            live=self.settings.live_for(venue),
            doctor_status=report.status.value,
            doctor_json=report.model_dump(mode="json"),
            checked_at=report.checked_at,
        )

    async def _choose_active(self, *, initial: bool = False) -> None:
        preferred = self.settings.active_venue
        if self.is_ready(preferred):
            self._active = preferred
        else:
            candidates = [v for v in self.enabled() if self.is_ready(v)]
            if candidates:
                self._active = candidates[0]
            elif self._active is not None and not self.is_ready(self._active):
                self._active = None
        if initial and self._active is not None and self._active is not preferred:
            logger.warning("active_venue_fallback", configured=preferred.value, active=self._active.value)
            await self.bus.publish(
                "venues.active_changed",
                venue=self._active.value,
                reason=f"configured venue {preferred.value} not ready",
                actor="startup",
            )

    async def switch(self, venue: VenueId, *, actor: str) -> dict[str, Any]:
        if venue not in self.enabled() and venue is not VenueId.FAKE:
            raise ValueError(f"venue {venue.value} is not enabled")
        if not self.is_ready(venue):
            raise ValueError(f"venue {venue.value} is not ready: {self.reason(venue)}")
        async with self._switch_lock:
            previous = self._active
            if previous is venue:
                return {"active": venue.value, "previous": previous.value if previous else None}
            await self.bus.publish(
                "venues.proposals_paused",
                reason="venue switch in progress",
                previous=previous.value if previous else None,
            )
            self._active = venue
            self.store.set_kv("active_venue", {"venue": venue.value, "at": utcnow().isoformat()})
            self.store.audit(actor, "venue.switch", {"from": previous.value if previous else None, "to": venue.value})
            await self.bus.publish(
                "venues.active_changed",
                venue=venue.value,
                previous=previous.value if previous else None,
                actor=actor,
            )
            logger.info(
                "venue_switched",
                previous=previous.value if previous else None,
                active=venue.value,
                actor=actor,
            )
            return {"active": venue.value, "previous": previous.value if previous else None}

    def summary(self) -> dict[str, Any]:
        active = self._active
        return {
            "active": active.value if active else None,
            "mode": self.mode(active).value if active else None,
            "venues": [
                {
                    "venue": s.venue.value,
                    "display_name": s.display_name,
                    "enabled": s.enabled,
                    "ready": s.ready,
                    "reason": s.reason,
                    "live": s.live,
                    "mode": s.mode.value,
                    "doctor_status": s.doctor.status.value if s.doctor else "NOT_RUN",
                }
                for s in self.statuses()
            ],
        }

    async def active_books(self, refs: list[MarketRef]) -> list[Any]:
        if not refs:
            return []
        adapter = self.for_active()
        return [await adapter.get_book(ref) for ref in refs]
