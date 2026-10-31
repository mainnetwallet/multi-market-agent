"""Deterministic in-memory venue used by every core test and the demo UI."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any

from mm_agent.config import Settings
from mm_agent.domain import (
    Balance,
    BookLevel,
    BookSnapshot,
    CancelResult,
    DoctorCheck,
    DoctorReport,
    DoctorStatus,
    ExecutionStatus,
    FeeQuote,
    Market,
    MarketRef,
    MarketStatus,
    OrderRequest,
    OrderResult,
    OrderType,
    Outcome,
    OutcomeInfo,
    Position,
    Side,
    VenueCaps,
    VenueId,
    VenueOrder,
    VenueTrade,
    quantize_shares,
    utcnow,
)
from mm_agent.venues.base import MarketQuery, Page, Venue

ZERO = Decimal("0")
ONE = Decimal("1")


class FakeVenue(Venue):
    id = VenueId.FAKE
    caps = VenueCaps(
        realtime=True,
        sandbox=True,
        min_order_shares=Decimal("1"),
        min_notional_usd=Decimal("1"),
        default_tick=Decimal("0.01"),
        order_types=(OrderType.GTC, OrderType.GTD, OrderType.FAK, OrderType.FOK),
        post_only=True,
        amend=True,
        batch_orders=True,
        approvals_needed=False,
        collateral_asset="USD",
        deposit=True,
        withdraw=True,
        redeem=True,
        funds_history=True,
    )

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings
        self._markets: dict[str, Market] = {}
        self._books: dict[str, BookSnapshot] = {}
        self._subscribers: list[asyncio.Queue[BookSnapshot]] = []
        self._orders: dict[str, VenueOrder] = {}
        self._order_requests: dict[str, OrderRequest] = {}
        self._positions: dict[str, tuple[Decimal, Decimal]] = {}
        self._trades: list[VenueTrade] = []
        self._cash = Decimal("1000")
        self.fail_next = 0
        self.timeout_next = 0
        self.timeout_after_place = False
        self.rest_next = False
        self.fill_ratio = Decimal("1")
        self.calls: list[tuple[str, Any]] = []
        self._seed()

    # ---------------------------------------------------------------- setup
    def _seed(self) -> None:
        market = Market(
            venue=VenueId.FAKE,
            native_id="fed-cut",
            title="Will the Fed cut rates at the next meeting?",
            category="Economics",
            event_id="fake-event-fed",
            event_title="Fed decision",
            end_time=utcnow() + timedelta(days=20),
            status=MarketStatus.OPEN,
            tick_size=Decimal("0.01"),
            outcomes=(
                OutcomeInfo(outcome=Outcome.YES, native_id="yes-token", label="Yes"),
                OutcomeInfo(outcome=Outcome.NO, native_id="no-token", label="No"),
            ),
            fee_info={"taker_rate": "0.05"},
            url="https://example.invalid/fake-fed-cut",
        )
        self._markets[market.native_id] = market
        self._books[market.outcome_ref(Outcome.YES)] = BookSnapshot(
            venue=VenueId.FAKE,
            market_id=market.native_id,
            outcome=Outcome.YES,
            bids=(BookLevel(price=Decimal("0.41"), size=Decimal("1000")),),
            asks=(
                BookLevel(price=Decimal("0.43"), size=Decimal("200")),
                BookLevel(price=Decimal("0.45"), size=Decimal("1000")),
            ),
            source="fake",
        )
        self._books[market.outcome_ref(Outcome.NO)] = BookSnapshot(
            venue=VenueId.FAKE,
            market_id=market.native_id,
            outcome=Outcome.NO,
            bids=(BookLevel(price=Decimal("0.55"), size=Decimal("900")),),
            asks=(
                BookLevel(price=Decimal("0.57"), size=Decimal("500")),
                BookLevel(price=Decimal("0.59"), size=Decimal("500")),
            ),
            source="fake",
        )

    @property
    def display_name(self) -> str:
        return "Fake"

    def add_market(self, market: Market, book_yes: BookSnapshot, book_no: BookSnapshot) -> None:
        self._markets[market.native_id] = market
        self._books[market.outcome_ref(Outcome.YES)] = book_yes
        self._books[market.outcome_ref(Outcome.NO)] = book_no

    def replace_book(self, ref: MarketRef, book: BookSnapshot) -> None:
        if ref.outcome is None:
            raise ValueError("outcome required")
        self._books[ref.namespaced] = book
        for queue in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(book)

    def set_book(
        self,
        market_id: str,
        outcome: Outcome,
        bids: list[tuple[str, str]],
        asks: list[tuple[str, str]],
    ) -> BookSnapshot:
        snapshot = BookSnapshot(
            venue=VenueId.FAKE,
            market_id=market_id,
            outcome=outcome,
            bids=tuple(BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in bids),
            asks=tuple(BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in asks),
            source="fake",
        )
        self.replace_book(MarketRef(venue=VenueId.FAKE, market_id=market_id, outcome=outcome), snapshot)
        return snapshot

    def reset_counters(self) -> None:
        self.fail_next = 0
        self.timeout_next = 0
        self.timeout_after_place = False
        self.rest_next = False
        self.fill_ratio = Decimal("1")

    # -------------------------------------------------------------- protocol
    async def doctor(self) -> DoctorReport:
        checks = (
            DoctorCheck(name="public_read", status=DoctorStatus.OK, detail="2 seed markets"),
            DoctorCheck(name="authenticated_read", status=DoctorStatus.OK, detail="fake balance"),
            DoctorCheck(name="order_roundtrip", status=DoctorStatus.OK, detail="fake venue"),
        )
        return DoctorReport(venue=self.id, status=DoctorStatus.OK, checks=checks)

    async def list_markets(self, query: MarketQuery) -> Page[Market]:
        items = list(self._markets.values())
        if query.q:
            needle = query.q.lower()
            items = [m for m in items if needle in m.title.lower()]
        if not query.include_closed:
            items = [m for m in items if m.status is MarketStatus.OPEN]
        return Page(items=items[: query.limit])

    async def get_market(self, ref: MarketRef) -> Market:
        market = self._markets.get(ref.market_id)
        if market is None:
            raise KeyError(f"fake market {ref.market_id} not found")
        return market

    async def get_book(self, ref: MarketRef, depth: int = 20) -> BookSnapshot:
        if ref.outcome is None:
            raise ValueError("outcome required")
        book = self._books.get(ref.namespaced)
        if book is None:
            raise KeyError(f"fake book {ref.namespaced} not found")
        return book.model_copy(update={"asks": book.asks[:depth], "bids": book.bids[:depth]})

    async def subscribe_books(self, refs: Sequence[MarketRef]) -> AsyncIterator[BookSnapshot]:
        queue: asyncio.Queue[BookSnapshot] = asyncio.Queue(maxsize=100)
        self._subscribers.append(queue)
        try:
            for ref in refs:
                book = await self.get_book(ref)
                yield book
            while True:
                yield await queue.get()
        finally:
            self._subscribers.remove(queue)

    async def get_balance(self) -> Balance:
        return Balance(venue=self.id, available_usd=self._cash, total_usd=self._cash)

    async def get_positions(self) -> list[Position]:
        market = self._markets.get("fed-cut")
        out: list[Position] = []
        for key, (shares, avg) in self._positions.items():
            if shares <= 0:
                continue
            _, _, outcome = key.split(":", 2)
            out.append(
                Position(
                    venue=self.id,
                    market_id="fed-cut",
                    outcome=Outcome(outcome),
                    shares=shares,
                    avg_price=avg,
                    market_title=market.title if market else None,
                )
            )
        return out

    async def get_open_orders(self, ref: MarketRef | None = None) -> list[VenueOrder]:
        orders = [o for o in self._orders.values() if o.status in {"OPEN", "PARTIAL"}]
        if ref:
            orders = [o for o in orders if o.market_id == ref.market_id]
        return orders

    async def get_recent_trades(self, ref: MarketRef) -> list[VenueTrade]:
        return [t for t in self._trades if t.market_id == ref.market_id]

    async def place_order(self, req: OrderRequest) -> OrderResult:
        self.calls.append(("place_order", req))
        if self.fail_next > 0:
            self.fail_next -= 1
            return OrderResult(
                venue=self.id,
                client_order_id=req.client_order_id,
                status=ExecutionStatus.FAILED,
                requested_size=req.size,
                message="fake injected failure",
            )
        timeout = False
        if self.timeout_next > 0:
            self.timeout_next -= 1
            timeout = True

        book = await self.get_book(MarketRef(venue=self.id, market_id=req.market_id, outcome=req.outcome))
        levels = book.asks if req.side is Side.BUY else book.bids
        remaining = req.size
        filled = ZERO
        notional = ZERO
        mutated: list[BookLevel] = []
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
            if level.size > take:
                mutated.append(BookLevel(price=level.price, size=level.size - take))
            if remaining <= 0:
                break
        # Resting orders do not cross in this fake, unless rest_next is requested.
        if filled > 0 and remaining <= 0 and self.fill_ratio < ONE:
            filled = quantize_shares(filled * self.fill_ratio, 4)
            remaining = req.size - filled
        avg = notional / filled if filled > 0 else None

        venue_order_id = "fake-" + req.client_order_id[-12:]
        status: ExecutionStatus
        if timeout:
            status = ExecutionStatus.UNKNOWN
        elif filled <= 0:
            status = ExecutionStatus.NOT_FILLED
        elif remaining <= 0:
            status = ExecutionStatus.FILLED
        else:
            status = ExecutionStatus.PARTIAL

        if filled > 0:
            self._apply_fill(req, filled, avg or req.price, venue_order_id)
            self._books[book.key] = book.model_copy(
                update={
                    "asks": tuple(mutated) if req.side is Side.BUY else book.asks,
                    "bids": tuple(mutated) if req.side is Side.SELL else book.bids,
                }
            )
        if remaining > 0 and (self.rest_next or status is ExecutionStatus.UNKNOWN):
            self._open(venue_order_id, req, remaining)
            if status is ExecutionStatus.UNKNOWN:
                status = ExecutionStatus.RESTING if not timeout else ExecutionStatus.UNKNOWN
        self._order_requests[venue_order_id] = req
        return OrderResult(
            venue=self.id,
            client_order_id=req.client_order_id,
            venue_order_id=None if timeout else venue_order_id,
            status=status,
            requested_size=req.size,
            filled_size=filled,
            remaining_size=remaining,
            average_price=avg,
            fee_usd=self.fee(req.price, filled, req.side, self._markets[req.market_id]).fee_usd,
            message="fake timeout" if timeout else "",
            raw_redacted={"fake": True},
        )

    def _apply_fill(self, req: OrderRequest, size: Decimal, price: Decimal, order_id: str) -> None:
        self._trades.append(
            VenueTrade(
                venue=self.id,
                market_id=req.market_id,
                trade_id=f"fake-trade-{len(self._trades) + 1}",
                order_id=order_id,
                client_order_id=req.client_order_id,
                outcome=req.outcome,
                side=req.side,
                price=price,
                size=size,
                created_at=utcnow(),
            )
        )
        key = f"{self.id.value}:{req.market_id}:{req.outcome.value}"
        old_shares, old_avg = self._positions.get(key, (ZERO, ZERO))
        if req.side is Side.BUY:
            new_shares = old_shares + size
            new_avg = (old_shares * old_avg + size * price) / new_shares
            self._positions[key] = (new_shares, new_avg)
            self._cash -= size * price
        else:
            self._positions[key] = (max(ZERO, old_shares - size), old_avg)
            self._cash += size * price

    def _open(self, order_id: str, req: OrderRequest, remaining: Decimal) -> None:
        self._orders[order_id] = VenueOrder(
            venue=self.id,
            market_id=req.market_id,
            order_id=order_id,
            client_order_id=req.client_order_id,
            outcome=req.outcome,
            side=req.side,
            price=req.price,
            size=req.size,
            filled=req.size - remaining,
            status="OPEN",
        )

    async def cancel_order(self, order_id: str) -> CancelResult:
        self.calls.append(("cancel_order", order_id))
        order = self._orders.pop(order_id, None)
        if order is None:
            return CancelResult(venue=self.id, ok=False, message="not open")
        return CancelResult(venue=self.id, ok=True, cancelled=1)

    async def cancel_all(self, ref: MarketRef | None = None) -> CancelResult:
        self.calls.append(("cancel_all", ref))
        targets = [oid for oid, o in self._orders.items() if ref is None or o.market_id == ref.market_id]
        for oid in targets:
            self._orders.pop(oid, None)
        return CancelResult(venue=self.id, ok=True, cancelled=len(targets))

    def fee(self, price: Decimal, size: Decimal, side: Side, market: Market) -> FeeQuote:
        del side  # the fake fills like a taker on both sides
        rate = Decimal(str(market.fee_info.get("taker_rate", "0.05")))
        return FeeQuote(
            fee_usd=size * rate * price * (ONE - price),
            rate=rate,
            source="fake taker formula",
        )

    async def redeem_positions(self, *, market_id: str, outcome: Any = None) -> dict[str, Any]:
        """The fake venue settles positions locally; nothing to redeem."""

        return {"supported": True, "settled": True, "market_id": market_id}

    async def get_price_history(self, market_id: str, *, interval: str = "1d") -> list[dict[str, Any]]:
        """A short synthetic series so the history route can be exercised."""

        from mm_agent.domain import utcnow

        now = utcnow()
        points: list[dict[str, Any]] = []
        for offset in range(5):
            price = Decimal("0.43") + Decimal(offset) / Decimal("100")
            points.append(
                {
                    "at": (now - timedelta(minutes=5 * (5 - offset))).isoformat(),
                    "open": str(price),
                    "high": str(price),
                    "low": str(price),
                    "close": str(price),
                    "volume": "0",
                }
            )
        return points
