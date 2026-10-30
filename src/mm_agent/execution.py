"""Order execution gateway, paper fills and UNKNOWN reconciliation."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx

from .books import BookCache
from .config import Settings
from .domain import (
    CancelResult,
    ExecutionStatus,
    Market,
    MarketRef,
    Mode,
    OrderRequest,
    OrderResult,
    OrderType,
    Outcome,
    Position,
    Side,
    VenueId,
    quantize_price,
    quantize_shares,
)
from .events import EventBus
from .locks import LockRegistry
from .store import Store
from .util import get_logger
from .venues.base import Venue
from .venues.registry import VenueRegistry

logger = get_logger(__name__)
ZERO = Decimal("0")


class ExecutionGateway:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        registry: VenueRegistry,
        books: BookCache,
        locks: LockRegistry,
        bus: EventBus,
    ) -> None:
        self.settings = settings
        self.store = store
        self.registry = registry
        self.books = books
        self.locks = locks
        self.bus = bus

    async def submit(
        self,
        req: OrderRequest,
        market: Market,
        *,
        proposal_id: str | None = None,
        client_ref: str | None = None,
    ) -> tuple[OrderResult, str]:
        adapter = self.registry.get(req.venue)
        local_id = self.store.create_order(
            venue=req.venue,
            mode=req.mode,
            market_id=req.market_id,
            outcome=req.outcome,
            side=req.side,
            price=req.price,
            size=req.size,
            client_order_id=req.client_order_id,
            proposal_id=proposal_id,
            reduce_only=req.reduce_only,
            order_type=req.order_type.value,
            post_only=req.post_only,
            client_ref=client_ref,
        )
        venue_lock = await self.locks.venue(req.venue)
        async with venue_lock:
            # Persist the intent-to-submit BEFORE any network side effect.
            if not self.store.transition_order(local_id, "SUBMITTING"):
                raise RuntimeError("could not persist SUBMITTING transition")
            if req.mode is Mode.PAPER:
                result = await self._paper(req, market, adapter)
            else:
                if not self.settings.live_for(req.venue):
                    result = OrderResult(
                        venue=req.venue,
                        client_order_id=req.client_order_id,
                        status=ExecutionStatus.FAILED,
                        requested_size=req.size,
                        message="LIVE refused: LIVE_<VENUE> is not enabled",
                    )
                else:
                    result = await self._live(req, adapter)
        await self._record_result(local_id, req, result, market)
        return result, local_id

    async def _paper(self, req: OrderRequest, market: Market, adapter: Venue) -> OrderResult:
        book = await self.books.get(
            adapter,
            MarketRef(venue=req.venue, market_id=req.market_id, outcome=req.outcome),
            force=True,
        )
        levels = book.asks if req.side is Side.BUY else book.bids
        remaining = req.size
        filled = ZERO
        notional = ZERO
        for level in levels:
            if req.side is Side.BUY and level.price > req.price:
                break
            if req.side is Side.SELL and level.price < req.price:
                break
            take = min(level.size, remaining)
            if take <= 0:
                continue
            filled += take
            notional += take * level.price
            remaining -= take
            if remaining <= 0:
                break
        avg = notional / filled if filled > 0 else None
        if filled <= 0:
            status = ExecutionStatus.NOT_FILLED
        elif remaining <= 0:
            status = ExecutionStatus.FILLED
        else:
            status = ExecutionStatus.PARTIAL
        fee = adapter.fee(avg or req.price, filled, req.side, market).fee_usd if filled > 0 else ZERO
        return OrderResult(
            venue=req.venue,
            client_order_id=req.client_order_id,
            venue_order_id="paper-" + req.client_order_id,
            status=status,
            requested_size=req.size,
            filled_size=filled,
            remaining_size=max(ZERO, remaining),
            average_price=avg,
            fee_usd=fee,
            message="paper fill against real books" if filled > 0 else "paper: no fill at limit",
            raw_redacted={"paper": True, "book_timestamp": book.timestamp.isoformat()},
        )

    async def _live(self, req: OrderRequest, adapter: Venue) -> OrderResult:
        try:
            return await adapter.place_order(req)
        except (TimeoutError, httpx.TimeoutException):
            return OrderResult(
                venue=req.venue,
                client_order_id=req.client_order_id,
                status=ExecutionStatus.UNKNOWN,
                requested_size=req.size,
                remaining_size=req.size,
                message="timeout; do not resend",
            )
        except Exception as exc:
            return OrderResult(
                venue=req.venue,
                client_order_id=req.client_order_id,
                status=ExecutionStatus.UNKNOWN,
                requested_size=req.size,
                remaining_size=req.size,
                message=f"uncertain {type(exc).__name__}",
            )

    async def _record_result(self, local_id: str, req: OrderRequest, result: OrderResult, market: Market) -> None:
        state = {
            ExecutionStatus.FILLED: "FILLED",
            ExecutionStatus.PARTIAL: "PARTIAL",
            ExecutionStatus.RESTING: "OPEN",
            ExecutionStatus.NOT_FILLED: "NOT_FILLED",
            ExecutionStatus.CANCELLED: "CANCELLED",
            ExecutionStatus.FAILED: "FAILED",
            ExecutionStatus.UNKNOWN: "UNKNOWN",
            ExecutionStatus.ERROR: "ERROR",
        }[result.status]
        self.store.transition_order(
            local_id,
            state,
            venue_order_id=result.venue_order_id,
            fee_usd=result.fee_usd if result.fee_usd > 0 else None,
            error=result.message
            if result.status in {ExecutionStatus.UNKNOWN, ExecutionStatus.ERROR, ExecutionStatus.FAILED}
            else None,
            raw=result.raw_redacted,
        )
        if result.filled_size > 0 and result.average_price is not None:
            fill_id = f"{result.venue_order_id or req.client_order_id}-{result.filled_size}-{result.average_price}"
            inserted = self.store.apply_fill(
                order_id=local_id,
                venue=req.venue,
                mode=req.mode,
                market_id=req.market_id,
                outcome=req.outcome,
                side=req.side,
                price=result.average_price,
                size=result.filled_size,
                fee_usd=result.fee_usd,
                venue_fill_id=fill_id,
                market_title=market.title,
            )
            if inserted:
                self.store.update_position_price(req.venue, req.market_id, req.outcome, result.average_price)
        await self.bus.publish(
            "order.updated",
            order=result.model_dump(mode="json"),
            local_order_id=local_id,
            mode=req.mode.value,
        )

    async def cancel(self, order_id: str, *, actor: str) -> dict[str, Any]:
        local = self.store.get_order(order_id)
        if local is None:
            raise ValueError(f"order {order_id} not found")
        if local["state"] in {"FILLED", "NOT_FILLED", "FAILED", "CANCELLED"}:
            return {"ok": False, "message": f"order is {local['state']}", "order": local}
        adapter = self.registry.get(VenueId(local["venue"]))
        venue_order_id = local.get("venue_order_id")
        if not venue_order_id:
            return {"ok": False, "message": "no venue order id; reconcile first", "order": local}
        result: CancelResult = await adapter.cancel_order(venue_order_id)
        if result.ok:
            self.store.transition_order(local["id"], "CANCELLED", error=None)
            self.store.audit(actor, "order.cancel", {"order_id": local["id"]})
        return {"ok": result.ok, "message": result.message, "cancelled": result.cancelled}

    async def cancel_all(self, *, actor: str, ref: MarketRef | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for venue in self.registry.enabled():
            if not self.registry.is_ready(venue):
                continue
            try:
                result = await self.registry.get(venue).cancel_all(ref)
            except Exception as exc:
                result = CancelResult(venue=venue, ok=False, message=f"{type(exc).__name__}")
            results.append(
                {
                    "venue": venue.value,
                    "ok": result.ok,
                    "cancelled": result.cancelled,
                    "message": result.message,
                }
            )
            if result.ok:
                for order in self.store.list_orders(venue=venue, states={"OPEN", "PARTIAL"}):
                    self.store.transition_order(order["id"], "CANCELLED")
        self.store.audit(actor, "orders.cancel_all", {"market": ref.namespaced if ref else None})
        return results

    async def reconcile_unknown(self, local_order_id: str, *, actor: str = "reconciler") -> dict[str, Any]:
        local = self.store.get_order(local_order_id)
        if local is None:
            raise ValueError("order not found")
        if local["state"] != "UNKNOWN":
            return {"resolved": True, "state": local["state"]}
        ref = MarketRef(
            venue=VenueId(local["venue"]),
            market_id=local["market_id"],
            outcome=Outcome(local["outcome"]),
        )
        try:
            open_orders = await self.registry.get(ref.venue).get_open_orders(ref)
        except Exception as exc:
            return {"resolved": False, "reason": f"open-order lookup failed: {type(exc).__name__}"}
        match = None
        for order in open_orders:
            if local.get("venue_order_id") and order.order_id == local["venue_order_id"]:
                match = order
                break
            if order.client_order_id and order.client_order_id == local["client_order_id"]:
                match = order
                break
        if match is not None:
            local_filled = Decimal(str(local.get("filled") or "0"))
            delta = max(Decimal("0"), match.filled - local_filled)
            state = "PARTIAL" if match.filled > 0 else "OPEN"
            if local["state"] == "UNKNOWN":
                self.store.transition_order(local["id"], state, venue_order_id=match.order_id, reconciled=True)
            if delta > 0:
                try:
                    market = await self.registry.get(ref.venue).get_market(ref)
                    fee = self.registry.get(ref.venue).fee(match.price, delta, match.side, market).fee_usd
                    title = market.title
                except Exception:
                    fee = Decimal("0")
                    title = None
                self.store.apply_fill(
                    order_id=local["id"],
                    venue=ref.venue,
                    mode=Mode(local["mode"]),
                    market_id=local["market_id"],
                    outcome=Outcome(local["outcome"]),
                    side=match.side,
                    price=match.price,
                    size=delta,
                    fee_usd=fee,
                    venue_fill_id=f"recon-open-{match.order_id}-{match.filled}",
                    market_title=title,
                )
            self.store.audit(
                actor,
                "order.reconciled",
                {"order_id": local["id"], "state": state, "filled_delta": str(delta)},
            )
            return {
                "resolved": True,
                "state": state,
                "venue_order_id": match.order_id,
                "filled_delta": str(delta),
            }
        # No open order. Check terminal trade history before leaving UNKNOWN.
        try:
            trades = await self.registry.get(ref.venue).get_recent_trades(ref)
        except Exception as exc:
            return {"resolved": False, "reason": f"trade-history lookup failed: {type(exc).__name__}"}
        matches = [
            trade
            for trade in trades
            if (local.get("venue_order_id") and trade.order_id and trade.order_id == local["venue_order_id"])
            or (trade.client_order_id and trade.client_order_id == local["client_order_id"])
        ]
        if matches:
            total = sum((trade.size for trade in matches), start=Decimal("0"))
            notional = sum((trade.size * trade.price for trade in matches), start=Decimal("0"))
            fees = sum((trade.fee_usd for trade in matches), start=Decimal("0"))
            requested = Decimal(str(local["size"]))
            local_filled = Decimal(str(local.get("filled") or "0"))
            delta = max(Decimal("0"), total - local_filled)
            if delta > 0:
                self.store.apply_fill(
                    order_id=local["id"],
                    venue=ref.venue,
                    mode=Mode(local["mode"]),
                    market_id=local["market_id"],
                    outcome=Outcome(local["outcome"]),
                    side=matches[0].side,
                    price=notional / total,
                    size=delta,
                    fee_usd=fees,
                    venue_fill_id=f"history-{matches[0].trade_id}-{total}",
                )
            state = "FILLED" if total >= requested else "PARTIAL"
            if local["state"] == "UNKNOWN":
                self.store.transition_order(local["id"], state, reconciled=True, error=None)
            self.store.audit(
                actor,
                "order.reconciled",
                {"order_id": local["id"], "source": "trade_history", "state": state},
            )
            return {
                "resolved": True,
                "state": state,
                "source": "trade_history",
                "filled_total": str(total),
            }
        # Absence from both open orders and trade history is still not proof of
        # no fill; leave UNKNOWN and block the market pending human evidence.
        self.store.audit(actor, "order.reconcile_unresolved", {"order_id": local["id"]})
        return {
            "resolved": False,
            "reason": "not found in open orders or trade history; human review required",
        }

    async def exit_position(
        self,
        position: Position,
        *,
        actor: str,
        reason: str,
        slippage: Decimal = Decimal("0.02"),
    ) -> tuple[OrderResult, str]:
        adapter = self.registry.get(position.venue)
        ref = MarketRef(venue=position.venue, market_id=position.market_id, outcome=position.outcome)
        book = await self.books.get(adapter, ref, force=True)
        if book.best_bid is None:
            raise RuntimeError("cannot exit: no bid")
        limit = book.best_bid - slippage
        if limit <= 0:
            raise RuntimeError("cannot exit: slippage cap consumes bid")
        market = await adapter.get_market(ref)
        limit = quantize_price(limit, market.tick_size)
        request = OrderRequest(
            venue=position.venue,
            market_id=position.market_id,
            outcome=position.outcome,
            side=Side.SELL,
            price=limit,
            size=quantize_shares(position.shares, 2),
            client_order_id=f"{position.venue.value[:3]}-exit-{uuid.uuid4().hex[:20]}",
            reduce_only=True,
            order_type=OrderType.FAK,
            mode=self.registry.mode(position.venue),
        )
        self.store.audit(
            actor, "position.exit", {"reason": reason, "ref": ref.namespaced, "size": str(position.shares)}
        )
        return await self.submit(request, market)
