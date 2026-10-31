"""Keep the app's view of each venue account equal to the venue's own.

Until now ``/v1/positions`` and ``/v1/orders`` were read from the local database, and the database was
written only when *this agent* placed an order. Anything done elsewhere - a trade on polymarket.com, an
order cancelled there, a fill that landed after the agent stopped looking - never reached the app.

This service makes the venue the source of truth:

* the active venue's positions, resting orders and balance are read every few seconds (other venues less
  often), and the store is made to match;
* an adapter that has a native account stream (Polymarket's authenticated user channel) wakes the sync the
  moment the venue reports an order or trade, so the wait is the network, not a poll interval;
* our own order events wake it too, so a fill is confirmed against the venue straight away;
* every real change is published as ``account.updated`` on the event bus, which the WebSocket forwards to
  the PWA and the Android app so they redraw at once.

Nothing here sends an order. It only reads the venue and writes the local mirror.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import timedelta
from decimal import Decimal
from typing import Any

from .config import Settings
from .domain import VenueId, utcnow
from .events import EventBus
from .store import Store
from .util import get_logger
from .venues.registry import VenueRegistry

logger = get_logger(__name__)

# One read of the venue must not hang the loop for long.
READ_TIMEOUT_SECONDS = 20.0
# An order of ours touched this recently has not been settled into the position yet; leave that market alone.
PROTECT_RECENT_SECONDS = 20
# A burst of events (a multi-fill trade) becomes one read.
MIN_GAP_SECONDS = 0.4
MAX_BACKOFF_SECONDS = 30.0
# States in which the agent still owes the position a fill it has not applied.
UNSETTLED_STATES = {"CREATED", "SUBMITTING", "UNKNOWN", "ERROR"}


class AccountSync:
    def __init__(self, settings: Settings, store: Store, registry: VenueRegistry, bus: EventBus) -> None:
        self.settings = settings
        self.store = store
        self.registry = registry
        self.bus = bus
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._wake: dict[VenueId, asyncio.Event] = {}
        self._locks: dict[VenueId, asyncio.Lock] = {}
        self._last_balance: dict[VenueId, tuple[str, str]] = {}
        self._diag: dict[str, dict[str, Any]] = {}
        self._streaming: set[VenueId] = set()
        # A venue's first read after start is the baseline: what it already holds is not "new".
        self._baselined: set[VenueId] = set()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self.settings.account_sync_enabled or self._tasks:
            return
        self._stop = asyncio.Event()
        for venue in self.registry.enabled():
            self._wake[venue] = asyncio.Event()
            self._locks[venue] = asyncio.Lock()
            self._diag[venue.value] = {
                "syncs": 0,
                "errors": 0,
                "last_error": None,
                "last_sync": None,
                "last_change": None,
                "streaming": False,
                "stream_error": None,
            }
            self._tasks.append(asyncio.create_task(self._venue_loop(venue), name=f"account-sync-{venue.value}"))
            self._tasks.append(asyncio.create_task(self._stream_loop(venue), name=f"account-stream-{venue.value}"))
        self._tasks.append(asyncio.create_task(self._bus_loop(), name="account-sync-bus"))

    async def stop(self) -> None:
        self._stop.set()
        for event in self._wake.values():
            event.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, diag in self._diag.items():
            entry = dict(diag)
            entry["streaming"] = VenueId(name) in self._streaming
            out[name] = entry
        return {
            "enabled": self.settings.account_sync_enabled,
            "running": any(not t.done() for t in self._tasks),
            "venues": out,
        }

    def trigger(self, venue: VenueId) -> None:
        """Ask for a read of this venue now."""

        event = self._wake.get(venue)
        if event is not None:
            event.set()

    # ----------------------------------------------------------------- loops
    def _interval(self, venue: VenueId) -> float:
        if self.registry.active_or_none == venue:
            return max(0.5, float(self.settings.account_sync_interval_seconds))
        return max(2.0, float(self.settings.account_sync_other_interval_seconds))

    async def _venue_loop(self, venue: VenueId) -> None:
        failures = 0
        wake = self._wake[venue]
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                if self.registry.is_ready(venue):
                    await self.sync_once(venue)
                    failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                diag = self._diag[venue.value]
                diag["errors"] += 1
                diag["last_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                logger.warning("account_sync_failed", venue=venue.value, error=diag["last_error"])
            wait = self._interval(venue)
            if failures:
                wait = min(MAX_BACKOFF_SECONDS, wait * (2 ** min(failures, 5)))
            wait = max(MIN_GAP_SECONDS, wait - (time.monotonic() - started))
            try:
                await asyncio.wait_for(wake.wait(), timeout=wait)
            except TimeoutError:
                continue
            # Woken by an event, not by the clock. Debounce: a burst of stream events is one read.
            await self._sleep(MIN_GAP_SECONDS)
            wake.clear()

    async def _stream_loop(self, venue: VenueId) -> None:
        """Wake the sync from the venue's own account stream, when it has one."""

        backoff = 2.0
        while not self._stop.is_set():
            if not self.registry.is_ready(venue):
                await self._sleep(5.0)
                continue
            try:
                stream = self.registry.get(venue).account_events()
                if stream is None:
                    return
                self._streaming.add(venue)
                backoff = 2.0
                async for _ in stream:
                    self._diag.setdefault(venue.value, {})["stream_error"] = None
                    self.trigger(venue)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc)[:160]}"
                self._diag.setdefault(venue.value, {})["stream_error"] = error
                logger.warning("account_stream_failed", venue=venue.value, error=error)
            finally:
                self._streaming.discard(venue)
            # The stream ended or failed: read once now (something may have happened meanwhile), then retry.
            self.trigger(venue)
            await self._sleep(backoff)
            backoff = min(MAX_BACKOFF_SECONDS, backoff * 2)

    async def _bus_loop(self) -> None:
        """Our own order events: confirm the result against the venue right away."""

        try:
            async with self.bus.subscribe() as stream:
                async for event in stream:
                    if event.topic not in {"order.updated", "order.cancelled", "position.closed"}:
                        continue
                    order = event.payload.get("order")
                    venue_name = event.payload.get("venue") or (order.get("venue") if isinstance(order, dict) else None)
                    try:
                        self.trigger(VenueId(str(venue_name)))
                    except ValueError:
                        for venue in self._wake:
                            self.trigger(venue)
        except asyncio.CancelledError:
            raise

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    # ------------------------------------------------------------------ sync
    def _protected(self, venue: VenueId) -> frozenset[tuple[str, str]]:
        """Markets where an order of ours is still being settled, so the venue read must not overwrite them."""

        cutoff = utcnow() - timedelta(seconds=PROTECT_RECENT_SECONDS)
        keys: set[tuple[str, str]] = set()
        for order in self.store.list_orders(venue=venue, limit=200):
            if str(order["client_order_id"]).startswith("ext:"):
                continue
            updated = order.get("updated_at")
            if order["state"] in UNSETTLED_STATES or (updated is not None and updated > cutoff):
                keys.add((str(order["market_id"]), str(order["outcome"])))
        return frozenset(keys)

    async def sync_once(self, venue: VenueId) -> dict[str, Any]:
        """Read the venue and make the store match. Returns what changed."""

        lock = self._locks.setdefault(venue, asyncio.Lock())
        async with lock:
            adapter = self.registry.get(venue)
            positions, orders, positions_complete, orders_complete = await asyncio.wait_for(
                adapter.get_account_snapshot(), timeout=READ_TIMEOUT_SECONDS
            )
            result = self.store.sync_positions(
                venue,
                positions,
                complete=positions_complete,
                protect=self._protected(venue),
            )
            touched = self.store.sync_open_orders(
                venue,
                orders,
                complete=orders_complete,
                moved=result["moved"] if positions_complete else None,
            )
            external = self._record_external(venue, result["deltas"])
            balance = await self._balance(venue)
            changed_positions = len(result["changed"])
            diag = self._diag.setdefault(
                venue.value,
                {
                    "syncs": 0,
                    "errors": 0,
                    "last_error": None,
                    "last_sync": None,
                    "last_change": None,
                    "streaming": False,
                },
            )
            diag["syncs"] += 1
            diag["last_error"] = None
            diag["last_sync"] = utcnow().isoformat()
            if changed_positions or touched or balance is not None:
                diag["last_change"] = diag["last_sync"]
                if result["moved"] or touched:
                    self.store.audit(
                        "account_sync",
                        "account.changed",
                        {
                            "venue": venue.value,
                            "positions": [f"{m}:{o}" for m, o in sorted(result["moved"])][:20],
                            "orders": len(touched),
                        },
                    )
                await self.bus.publish(
                    "account.updated",
                    venue=venue.value,
                    positions_changed=changed_positions,
                    orders_changed=len(touched),
                    balance=balance,
                )
            if external:
                await self.bus.publish("account.external_change", venue=venue.value, trades=external)
            return {
                "venue": venue.value,
                "positions_changed": changed_positions,
                "orders_changed": len(touched),
                "balance_changed": balance is not None,
            }

    def _record_external(self, venue: VenueId, deltas: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Position changes the agent did not make: add them to the trade history, return them for the alert.

        The first read after start only sets the baseline, so what the venue already held when the server
        came up is not reported as new trades.
        """

        if venue not in self._baselined:
            self._baselined.add(venue)
            return []
        if not deltas:
            return []
        try:
            self.store.record_external_fills(venue, deltas)
        except Exception as exc:
            logger.warning("external_fill_record_failed", venue=venue.value, error=str(exc)[:160])
        self.store.audit(
            "account_sync",
            "account.external_change",
            {"venue": venue.value, "count": len(deltas)},
        )
        if not self.settings.account_sync_alert_external:
            return []
        return [
            {
                "market_id": str(d["market_id"]),
                "outcome": str(d["outcome"]),
                "title": str(d.get("title") or d["market_id"]),
                "side": "BUY" if Decimal(d["delta"]) > 0 else "SELL",
                "shares": str(abs(Decimal(d["delta"])).normalize()),
                "price": str(Decimal(d["price"]).quantize(Decimal("0.001"))),
                "position_after": str(Decimal(d["new"]).normalize()),
            }
            for d in deltas
        ]

    async def _balance(self, venue: VenueId) -> dict[str, str] | None:
        """The balance when it differs from the last read, else ``None`` (and a failed read is not an update)."""

        try:
            balance = await asyncio.wait_for(self.registry.get(venue).get_balance(), timeout=READ_TIMEOUT_SECONDS)
        except Exception:
            return None
        available = str(balance.available_usd)
        total = str(balance.total_usd if balance.total_usd is not None else balance.available_usd)
        try:
            same = (Decimal(self._last_balance[venue][0]) == Decimal(available)) and (
                Decimal(self._last_balance[venue][1]) == Decimal(total)
            )
        except (KeyError, ArithmeticError):
            same = False
        if same:
            return None
        self._last_balance[venue] = (available, total)
        return {"available": available, "total": total, "as_of": balance.fetched_at.isoformat()}
