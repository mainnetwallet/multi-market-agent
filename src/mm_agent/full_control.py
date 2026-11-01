"""Full-control layer: capabilities, manual trading, funds, step-up, idempotency.

Kept separate from ``agent.py`` so the agent's proposal flow stays untouched. Every
money-moving call here goes through the same risk engine, the same caps and the same
audit trail as an agent proposal; the operator's own order is their approval, but it
is still checked.

Invariants:
* a money-moving POST needs an ``Idempotency-Key``; a replay returns the first result.
* a step-up token is single-use, session-bound and action-bound, and expires in 2 min.
* UNKNOWN order outcomes are never resent.
* funds routes are off unless ``FUNDS_ENABLED=true``.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from .agent import AgentService
from .config import Settings
from .domain import (
    Balance,
    Market,
    MarketRef,
    MarketStatus,
    OrderRequest,
    OrderType,
    Outcome,
    Side,
    VenueId,
    quantize_shares,
    utcnow,
)
from .execution import ExecutionGateway
from .store import Store
from .util import get_logger
from .venues.base import VenueHTTPError
from .venues.registry import VenueRegistry

logger = get_logger(__name__)

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")

STEPUP_TTL_SECONDS = 120
STEPUP_ACTIONS = (
    "allowlist_add",
    "ceiling_raise",
    "approvals",
)

# Official venue pages, shown to the operator when a feature has no API.
VENUE_DOCS: dict[str, str] = {
    "polymarket": "https://docs.polymarket.com",
    "kalshi": "https://docs.kalshi.com",
    "limitless": "https://docs.limitless.exchange",
}


class FullControlError(Exception):
    """A refusal with a plain-language reason the operator can act on."""

    def __init__(self, message: str, *, code: str = "refused", status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


class FullControlService:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        registry: VenueRegistry,
        agent: AgentService,
        execution: ExecutionGateway,
    ) -> None:
        self.settings = settings
        self.store = store
        self.registry = registry
        self.agent = agent
        self.execution = execution

    # ------------------------------------------------------------ capabilities
    def capabilities(self) -> dict[str, Any]:
        """What the app may offer, per venue, and why something is unavailable."""

        venues: dict[str, Any] = {}
        for venue in self.registry.enabled():
            adapter = self.registry.get(venue)
            caps = adapter.caps
            venues[venue.value] = {
                "display_name": adapter.display_name,
                "ready": self.registry.is_ready(venue),
                "reason": self.registry.reason(venue),
                "collateral_asset": caps.collateral_asset,
                "order_types": [t.value for t in caps.order_types],
                "post_only": caps.post_only,
                "amend": caps.amend,
                "batch_orders": caps.batch_orders,
                "approvals_needed": caps.approvals_needed,
                "min_order_shares": str(caps.min_order_shares),
                "min_notional_usd": str(caps.min_notional_usd),
                "tick": str(caps.default_tick),
                "funds": {
                    "enabled": bool(self.settings.funds_enabled),
                    "deposit": caps.deposit,
                    "withdraw": caps.withdraw,
                    "redeem": caps.redeem,
                    "history": caps.funds_history,
                    # The bridge address and the transfer are separate steps; say so.
                    "withdraw_note": (
                        "The withdrawal sends the collateral to a bridge address; the bridge "
                        "then delivers it to the recipient."
                        if caps.withdraw
                        else ""
                    ),
                    "reason": "Funds are disabled; set FUNDS_ENABLED=true to enable them.",
                },
                "official_url": VENUE_DOCS.get(venue.value, ""),
                "unsupported": dict(caps.unsupported_reason),
            }
        return {
            "manual_orders": True,
            "sell": True,
            "redeem": any(v["funds"]["redeem"] for v in venues.values()),
            "cancel": True,
            "cancel_all": True,
            "heartbeat": {
                "venue": "polymarket",
                "enabled": False,
                "reason": (
                    "The Polymarket heartbeat has no disarm: the first accepted send enables "
                    "timeout cancellation for every open order, so a backend restart would "
                    "silently cancel resting orders. It is deliberately not used."
                ),
            },
            "venues": venues,
        }

    # ---------------------------------------------------------------- balance
    async def balance(self, venue: VenueId | None = None) -> dict[str, Any]:
        """Per-venue balance, read from the venue."""

        targets = [venue] if venue else list(self.registry.enabled())
        out: list[dict[str, Any]] = []
        for target in targets:
            adapter = self.registry.get(target)
            entry: dict[str, Any] = {
                "venue": target.value,
                "display_name": adapter.display_name,
                "collateral_asset": adapter.caps.collateral_asset,
                "available": None,
                "in_orders": None,
                "positions_value": None,
                "total": None,
                "as_of": None,
                "reason": None,
            }
            if not self.registry.is_ready(target):
                entry["reason"] = self.registry.reason(target) or "venue not ready"
                out.append(entry)
                continue
            try:
                balance: Balance = await adapter.get_balance()
            except Exception as exc:
                entry["reason"] = f"balance unavailable: {type(exc).__name__}"
                out.append(entry)
                continue
            in_orders = ZERO
            positions_value = ZERO
            for order in self.store.list_orders(venue=target, states={"OPEN", "PARTIAL"}, limit=200):
                filled = Decimal(str(order.get("filled") or "0"))
                size = Decimal(str(order["size"]))
                in_orders += (size - filled) * Decimal(str(order["price"]))
            for position in self.store.list_positions(venue=target):
                mark = position.current_price or position.avg_price
                positions_value += position.shares * mark
            entry["available"] = str(balance.available_usd)
            entry["in_orders"] = str(in_orders)
            entry["positions_value"] = str(positions_value)
            entry["total"] = str(balance.total_usd if balance.total_usd is not None else balance.available_usd)
            entry["as_of"] = balance.fetched_at.isoformat()
            out.append(entry)
        if venue:
            return out[0] if out else {"venue": venue.value, "available": None, "reason": "venue not enabled"}
        return {"balances": out}

    # ---------------------------------------------------------------- step-up
    def create_stepup(self, password: str, session_id: str, action: str) -> dict[str, Any]:
        """Issue a single-use step-up token. The password is never stored or logged."""

        if action not in STEPUP_ACTIONS:
            raise FullControlError(f"unknown step-up action {action}", code="bad_action", status=400)
        if not secrets.compare_digest(password, self.settings.admin_password or ""):
            raise FullControlError("incorrect password", code="bad_password", status=401)
        raw = secrets.token_urlsafe(32)
        token_hash = _hash("stepup", raw)
        expires_at = utcnow() + timedelta(seconds=STEPUP_TTL_SECONDS)
        self.store.stepup_create(token_hash, session_id, action, expires_at)
        self.store.audit(f"stepup:{action}", "auth.stepup", {"session": session_id})
        return {"stepup_token": raw, "expires_in": STEPUP_TTL_SECONDS, "action": action}

    def require_stepup(self, token: str | None, session_id: str, action: str) -> None:
        if not token:
            raise FullControlError("a step-up token is required for this action", code="stepup_required", status=403)
        token_hash = _hash("stepup", token)
        if not self.store.stepup_consume(token_hash, session_id, action):
            raise FullControlError(
                "the step-up token is expired, already used, or bound to another action",
                code="stepup_invalid",
                status=403,
            )

    # ------------------------------------------------------------ idempotency
    @staticmethod
    def request_hash(scope: str, body: dict[str, Any]) -> str:
        payload = json.dumps(body, sort_keys=True, default=str)
        return _hash("idem", scope, payload)

    def idempotent(self, key: str | None, scope: str, body: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """Return (stored_result, request_hash). A stored result means "replay"."""

        if not key:
            raise FullControlError(
                "this action moves money and requires an Idempotency-Key header",
                code="idempotency_required",
                status=400,
            )
        request_hash = self.request_hash(scope, body)
        return self.store.idempotent_get(key, scope, request_hash), request_hash

    def remember(self, key: str, scope: str, request_hash: str, response: dict[str, Any]) -> None:
        self.store.idempotent_put(key, scope, request_hash, response)

    # --------------------------------------------------------------- trading
    def _venue_or_active(self, venue: VenueId | None) -> VenueId:
        target = venue or self.registry.active_or_none
        if target is None:
            raise FullControlError("no active venue", code="no_venue", status=409)
        return target

    def _check_order_type(
        self,
        venue: VenueId,
        order_type: OrderType,
        post_only: bool,
        expires_at: Any = None,
    ) -> None:
        caps = self.registry.get(venue).caps
        if order_type not in caps.order_types:
            reason = caps.unsupported_reason.get(order_type.value.lower())
            raise FullControlError(
                reason or f"{venue.value} does not support {order_type.value} orders",
                code="unsupported_order_type",
                status=400,
            )
        if post_only and not caps.post_only:
            reason = caps.unsupported_reason.get("post_only")
            raise FullControlError(
                reason or f"{venue.value} does not support post-only orders",
                code="unsupported_post_only",
                status=400,
            )
        if post_only and order_type in (OrderType.FOK, OrderType.FAK, OrderType.IOC):
            raise FullControlError(
                "post-only only applies to resting orders, not to fill-or-kill types",
                code="post_only_conflict",
                status=400,
            )
        if order_type is OrderType.GTD:
            if expires_at is None:
                raise FullControlError(
                    "an order with an expiry needs an expiry time",
                    code="missing_expiry",
                    status=400,
                )
            floor = caps.min_expiry_minutes
            if floor:
                remaining = (expires_at - utcnow()).total_seconds() / 60
                if remaining < floor:
                    raise FullControlError(
                        f"the expiry must be at least {floor} minutes away",
                        code="expiry_too_soon",
                        status=400,
                    )

    async def _market_for(self, venue: VenueId, market_id: str) -> Market:
        try:
            return await self.agent.get_market(market_id, venue)
        except Exception as exc:
            raise FullControlError(f"market {market_id} not found: {exc}", code="market_not_found", status=404) from exc

    def _size_from_amount(self, amount_usd: Decimal | None, shares: Decimal | None, price: Decimal) -> Decimal:
        if shares is not None:
            return quantize_shares(shares, 4)
        if amount_usd is not None:
            if amount_usd <= 0:
                raise FullControlError("amount_usd must be positive", code="bad_amount", status=400)
            return quantize_shares(amount_usd / price, 4)
        raise FullControlError("provide either shares or amount_usd", code="missing_size", status=400)

    def _check_risk(self, venue: VenueId, amount: Decimal, *, market_id: str | None = None) -> None:
        """Manual orders pass the same caps as agent proposals."""

        bankroll = self.settings.limit_bankroll(venue)
        cap = self.agent.risk.caps["max_trade_usd"]
        if amount > cap:
            raise FullControlError(
                f"order of {amount} exceeds the per-trade cap of {cap}",
                code="max_trade_exceeded",
            )
        if self.store.today_pnl() <= -(bankroll * self.agent.risk.caps["max_daily_loss_pct"] / HUNDRED):
            raise FullControlError(
                "the daily loss limit is reached; no new entries",
                code="daily_loss_cap",
            )
        if (
            self.store.total_exposure(venue) + amount
            > bankroll * self.agent.risk.caps["max_total_exposure_pct"] / HUNDRED
        ):
            raise FullControlError(
                "the total exposure cap is reached; no new entries",
                code="exposure_cap",
            )
        # The same position-count cap the agent's own sizing applies. It counts
        # markets, not outcomes: YES and NO on one market are one position.
        max_positions = int(self.agent.risk.caps["max_open_positions"])
        positions = self.store.list_positions(venue=venue)
        open_markets = {p.market_id for p in positions}
        already_held = bool(market_id) and market_id in open_markets
        if len(open_markets) >= max_positions and not already_held:
            raise FullControlError(
                f"the limit of {max_positions} open positions is reached; no new entries",
                code="max_open_positions",
            )

    async def place_manual_order(
        self,
        *,
        venue: VenueId | None,
        market_id: str,
        outcome: Outcome,
        side: Side,
        price: Decimal | None,
        shares: Decimal | None,
        amount_usd: Decimal | None,
        order_type: OrderType,
        expires_at: Any = None,
        post_only: bool = False,
        client_id: str | None = None,
        actor: str,
        max_slippage_cents: Decimal | None = None,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        """Place an order the operator asked for by hand."""

        target = self._venue_or_active(venue)
        if not self.registry.is_ready(target):
            raise FullControlError(self.registry.reason(target) or "venue not ready", code="venue_not_ready")
        self._check_order_type(target, order_type, post_only, expires_at)
        market = await self._market_for(target, market_id)
        if market.status is not MarketStatus.OPEN:
            raise FullControlError(f"market is {market.status.value}", code="market_not_open")

        adapter = self.registry.get(target)
        book = await self.agent.books.get(
            adapter,
            MarketRef(venue=target, market_id=market_id, outcome=outcome),
            force=True,
        )
        if order_type in (OrderType.FOK, OrderType.FAK):
            if price is None:
                # Market order: derive the worst price from the book plus a slippage bound.
                slip = max_slippage_cents if max_slippage_cents is not None else Decimal("2")
                if side is Side.BUY:
                    if not book.asks:
                        raise FullControlError("no asks in the book; cannot price a market buy", code="no_book")
                    worst = book.asks[0].price + slip / HUNDRED
                else:
                    if not book.bids:
                        raise FullControlError("no bids in the book; cannot price a market sell", code="no_book")
                    worst = book.bids[0].price - slip / HUNDRED
                price = max(min(worst, ONE - Decimal("0.0001")), Decimal("0.0001"))
        elif price is None:
            raise FullControlError("a limit order needs a price", code="missing_price", status=400)

        if not (ZERO < price < ONE):
            raise FullControlError("price must be a probability in (0, 1)", code="bad_price", status=400)
        size = self._size_from_amount(amount_usd, shares, price)
        amount = (size * price).quantize(Decimal("0.01"))
        if not reduce_only:
            self._check_risk(target, amount, market_id=market_id)
        if size < adapter.caps.min_order_shares:
            raise FullControlError(
                f"{target.value} needs at least {adapter.caps.min_order_shares} shares",
                code="below_min_shares",
            )
        if amount < adapter.caps.min_notional_usd and not reduce_only:
            raise FullControlError(
                f"{target.value} needs at least {adapter.caps.min_notional_usd} of notional",
                code="below_min_notional",
            )
        if self.agent.paused:
            raise FullControlError("the agent is paused; resume before trading", code="agent_paused")

        client_order_id = client_id or f"{target.value[:3]}-{uuid.uuid4().hex[:22]}"
        request = OrderRequest(
            venue=target,
            market_id=market_id,
            outcome=outcome,
            side=side,
            price=price,
            size=size,
            client_order_id=client_order_id,
            reduce_only=reduce_only,
            order_type=order_type,
            expires_at=expires_at,
            post_only=post_only,
        )
        result, local_id = await self.execution.submit(request, market)
        self.store.audit(
            actor,
            "order.manual",
            {
                "order_id": local_id,
                "venue": target.value,
                "market_id": market_id,
                "side": side.value,
                "price": str(price),
                "shares": str(size),
                "status": result.status.value,
            },
        )
        await self.agent.bus.publish(
            "order.manual",
            order_id=local_id,
            venue=target.value,
            status=result.status.value,
        )
        return {
            "order_id": local_id,
            "venue_order_id": result.venue_order_id,
            "client_order_id": client_order_id,
            "venue": target.value,
            "market_id": market_id,
            "outcome": outcome.value,
            "side": side.value,
            "order_type": order_type.value,
            "price": str(price),
            "shares": str(size),
            "amount_usd": str(amount),
            "status": result.status.value,
            "filled": str(result.filled_size),
            "remaining": str(result.remaining_size),
            "average_price": str(result.average_price) if result.average_price else None,
            "fee_usd": str(result.fee_usd),
            "message": result.message,
        }

    async def sell_position(
        self,
        *,
        venue: VenueId | None,
        market_id: str,
        outcome: Outcome,
        shares: Decimal,
        price: Decimal | None,
        actor: str,
    ) -> dict[str, Any]:
        """Sell part or all of a position. Reduce-only: never opens a short."""

        target = self._venue_or_active(venue)
        position = next(
            (p for p in self.store.list_positions(venue=target) if p.market_id == market_id and p.outcome is outcome),
            None,
        )
        if position is None:
            raise FullControlError(f"no open {outcome.value} position on {market_id}", code="no_position", status=404)
        if shares <= 0:
            raise FullControlError("shares must be positive", code="bad_shares", status=400)
        if shares > position.shares:
            raise FullControlError(
                f"you hold {position.shares} shares; cannot sell {shares}",
                code="too_many_shares",
            )
        adapter = self.registry.get(target)
        book = await self.agent.books.get(
            adapter,
            MarketRef(venue=target, market_id=market_id, outcome=outcome),
            force=True,
        )
        if price is None:
            if not book.bids:
                raise FullControlError("no bid in the book; cannot price a market sell", code="no_book")
            price = book.bids[0].price
        market = await self._market_for(target, market_id)
        request = OrderRequest(
            venue=target,
            market_id=market_id,
            outcome=outcome,
            side=Side.SELL,
            price=price,
            size=quantize_shares(shares, 4),
            client_order_id=f"{target.value[:3]}-sell-{uuid.uuid4().hex[:20]}",
            reduce_only=True,
            order_type=OrderType.FAK,
        )
        result, local_id = await self.execution.submit(request, market)
        self.store.audit(
            actor,
            "position.sell",
            {"order_id": local_id, "venue": target.value, "market_id": market_id, "shares": str(shares)},
        )
        return {
            "order_id": local_id,
            "venue": target.value,
            "market_id": market_id,
            "outcome": outcome.value,
            "side": "SELL",
            "price": str(price),
            "shares": str(shares),
            "status": result.status.value,
            "filled": str(result.filled_size),
            "remaining": str(result.remaining_size),
            "average_price": str(result.average_price) if result.average_price else None,
            "fee_usd": str(result.fee_usd),
            "message": result.message,
        }

    async def amend_order(
        self,
        order_id: str,
        *,
        price: Decimal | None,
        shares: Decimal | None,
        actor: str,
    ) -> dict[str, Any]:
        """Amend a resting order where the venue supports it."""

        local = self.store.get_order(order_id)
        if local is None:
            raise FullControlError(f"order {order_id} not found", code="not_found", status=404)
        venue = VenueId(local["venue"])
        if not self.registry.get(venue).caps.amend:
            reason = self.registry.get(venue).caps.unsupported_reason.get("amend")
            raise FullControlError(
                reason or f"{venue.value} does not support amending orders",
                code="unsupported_amend",
                status=400,
            )
        venue_order_id = local.get("venue_order_id")
        if not venue_order_id:
            raise FullControlError("no venue order id; reconcile first", code="not_reconciled")
        result = await self.registry.get(venue).amend_order(venue_order_id, price=price, size=shares)
        self.store.audit(
            actor,
            "order.amend",
            {
                "order_id": local["id"],
                "price": str(price) if price else None,
                "shares": str(shares) if shares else None,
            },
        )
        return {
            "order_id": local["id"],
            "status": result.status.value,
            "filled": str(result.filled_size),
            "remaining": str(result.remaining_size),
            "average_price": str(result.average_price) if result.average_price else None,
            "message": result.message,
        }

    async def cancel_all_orders(
        self,
        *,
        venue: VenueId | None,
        market_id: str | None,
        actor: str,
    ) -> dict[str, Any]:
        """The kill switch. Never blocked by caps; idempotent; audited."""

        ref = MarketRef(venue=venue or self.registry.active, market_id=market_id) if market_id else None
        results = await self.execution.cancel_all(actor=actor, ref=ref)
        self.store.audit(
            actor,
            "orders.cancel_all",
            {"venue": venue.value if venue else None, "market_id": market_id, "results": results},
        )
        return {"results": results, "cancelled": sum(int(r.get("cancelled") or 0) for r in results)}

    # ---------------------------------------------------------------- history
    def trades(
        self,
        *,
        venue: VenueId | None,
        market_id: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        before = None
        if cursor:
            try:
                before = utcnow().fromisoformat(cursor)
            except ValueError:
                before = None
        rows = self.store.list_fills(venue=venue, market_id=market_id, limit=limit, before=before)
        next_cursor = rows[-1]["at"] if rows else None
        return {"trades": rows, "next_cursor": next_cursor}

    def pnl_daily(self, days: int = 30, *, venue: VenueId | None = None) -> dict[str, Any]:
        series = self.store.daily_pnl_series(days, venue=venue)
        unrealized = ZERO
        for position in self.store.list_positions(venue=venue):
            unrealized += position.unrealized_pnl
        return {"days": series, "unrealized_usd": str(unrealized)}

    async def market_book(
        self, market_id: str, *, venue: VenueId | None, outcome: Outcome | None, depth: int
    ) -> dict[str, Any]:
        target = self._venue_or_active(venue)
        market = await self._market_for(target, market_id)
        books = await self.agent.books_for(market, force=True)
        wanted = outcome or Outcome.YES
        book = books.get(wanted)
        if book is None:
            raise FullControlError(f"no book for {market_id}", code="no_book", status=404)
        return {
            "venue": target.value,
            "market_id": market_id,
            "outcome": wanted.value,
            "bids": [{"price": str(level.price), "shares": str(level.size)} for level in book.bids[:depth]],
            "asks": [{"price": str(level.price), "shares": str(level.size)} for level in book.asks[:depth]],
            "mid": str(book.mid) if book.mid is not None else None,
            "spread": str(book.spread) if book.spread is not None else None,
            "as_of": book.timestamp.isoformat(),
        }

    async def market_history(
        self, market_id: str, *, venue: VenueId | None, interval: str, outcome: Outcome = Outcome.YES
    ) -> dict[str, Any]:
        target = self._venue_or_active(venue)
        adapter = self.registry.get(target)
        try:
            if outcome == Outcome.YES:
                points = await adapter.get_price_history(market_id, interval=interval)
            elif getattr(adapter, "history_by_outcome", False):
                points = await adapter.get_price_history(market_id, interval=interval, outcome=outcome)
            else:  # a binary market: the NO price is one minus the YES price
                points = [
                    {
                        **p,
                        **{
                            k: str(Decimal(1) - Decimal(str(p[k])))
                            for k in ("open", "high", "low", "close")
                            if p.get(k) is not None
                        },
                    }
                    for p in await adapter.get_price_history(market_id, interval=interval)
                ]
        except ValueError as exc:
            raise FullControlError(str(exc), code="bad_interval", status=400) from exc
        except Exception as exc:
            reason = adapter.caps.unsupported_reason.get("price_history")
            detail = (
                f"HTTP {exc.status_code}: {exc.body[:160]}" if isinstance(exc, VenueHTTPError) else type(exc).__name__
            )
            raise FullControlError(
                reason or f"{target.value} price history is unavailable: {detail}",
                code="unsupported_history",
                status=400,
            ) from exc
        if not points:
            reason = adapter.caps.unsupported_reason.get("price_history")
            raise FullControlError(
                reason or f"{target.value} has no price history for {interval}",
                code="unsupported_history",
                status=400,
            )
        return {"venue": target.value, "market_id": market_id, "interval": interval, "points": points}

    # ------------------------------------------------------------- ceilings
    def set_hard_ceiling(
        self, name: str, value: Decimal, *, actor: str, raise_verified: bool = False
    ) -> dict[str, Any]:
        """Raise or lower a stored ceiling.

        Raising needs a step-up token; the caller passes ``raise_verified=True``
        once it has consumed one, so the rule lives in one place.
        """

        previous = self.agent.risk.ceilings.get(name)
        if previous is None:
            raise FullControlError(f"unknown hard ceiling {name}", code="unknown_ceiling", status=400)
        if value > previous and not raise_verified:
            raise FullControlError(
                f"raising {name} needs a step-up token",
                code="stepup_required",
                status=403,
            )
        try:
            effective = self.agent.risk.set_ceiling(name, value)
        except (ValueError, ArithmeticError) as exc:
            raise FullControlError(str(exc), code="bad_ceiling", status=400) from exc
        self.store.set_hard_ceiling(name, effective, actor)
        self.store.set_kv("hard_ceilings", self.agent.risk.ceilings_dict())
        self.store.audit(actor, "limits.ceiling", {"name": name, "value": str(effective)})
        return {"name": name, "value": str(effective), "hard_ceilings": self.agent.risk.ceilings_dict()}

    def restore_ceilings(self) -> None:
        stored = self.store.hard_ceilings()
        if stored:
            self.agent.risk.restore_ceilings({k: str(v) for k, v in stored.items()})
        else:
            default = self.settings.default_max_trade_usd
            self.agent.risk.set_ceiling("max_trade_usd", default)
            self.store.set_hard_ceiling("max_trade_usd", default, "system")

    # ---------------------------------------------------------------- redeem
    async def redeem(
        self, *, venue: VenueId | None, market_id: str, outcome: Outcome | None, actor: str
    ) -> dict[str, Any]:
        target = self._venue_or_active(venue)
        adapter = self.registry.get(target)
        if not adapter.caps.redeem:
            reason = adapter.caps.unsupported_reason.get("redeem")
            raise FullControlError(
                reason or f"{target.value} does not support redeeming through the API",
                code="unsupported_redeem",
                status=400,
            )
        result = await adapter.redeem_positions(market_id=market_id, outcome=outcome)
        self.store.audit(actor, "position.redeem", {"venue": target.value, "market_id": market_id})
        return {"venue": target.value, "market_id": market_id, "result": result}

    async def split_merge(self, *, venue: VenueId | None, market_id: str, action: str, actor: str) -> dict[str, Any]:
        """Split or merge a Polymarket position. Advanced and live-only."""

        target = self._venue_or_active(venue)
        if target is not VenueId.POLYMARKET:
            reason = self.registry.get(target).caps.unsupported_reason.get(action)
            raise FullControlError(
                reason or f"{target.value} does not support {action} through the API",
                code="unsupported_action",
                status=400,
            )
        result = await self.registry.get(target).split_merge(market_id=market_id, action=action)
        self.store.audit(actor, f"position.{action}", {"venue": target.value, "market_id": market_id})
        return {"venue": target.value, "market_id": market_id, "action": action, "result": result}
