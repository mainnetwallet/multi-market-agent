"""Polymarket adapter: Gamma discovery, CLOB books, official SDK for account/orders."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from ..config import Settings
from ..domain import (
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
    Outcome,
    OutcomeInfo,
    Position,
    Side,
    VenueCaps,
    VenueId,
    VenueOrder,
    normalize_probability,
    parse_iso,
    quantize_price,
    quantize_shares,
    to_decimal,
    utcnow,
)
from . import polymarket_signing
from .base import HttpVenue, MarketQuery, Page, VenueAuthError, VenueUnavailableError

ZERO = Decimal("0")
ONE = Decimal("1")
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

CATEGORY_FEE_RATES: dict[str, Decimal] = {
    "crypto": Decimal("0.07"),
    "sports": Decimal("0.05"),
    "finance": Decimal("0.04"),
    "politics": Decimal("0.04"),
    "economics": Decimal("0.05"),
    "culture": Decimal("0.05"),
    "weather": Decimal("0.05"),
    "mentions": Decimal("0.04"),
    "tech": Decimal("0.04"),
    "geopolitics": Decimal("0"),
    "other": Decimal("0.05"),
}


class PolymarketVenue(HttpVenue):
    id = VenueId.POLYMARKET
    caps = VenueCaps(
        realtime=True,
        sandbox=False,
        neg_risk=True,
        min_order_shares=Decimal("5"),
        min_notional_usd=Decimal("1"),
        default_tick=Decimal("0.01"),
    )

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._markets: dict[str, Market] = {}
        self._asset_index: dict[str, tuple[str, Outcome]] = {}
        self._condition_index: dict[str, str] = {}
        self.test_orders = False
        self._test_error: str | None = None

    @property
    def display_name(self) -> str:
        return "Polymarket"

    # ------------------------------------------------------------- discovery
    async def list_markets(self, query: MarketQuery) -> Page[Market]:
        offset = int(query.cursor) if query.cursor and query.cursor.isdigit() else 0
        limit = min(max(query.limit, 1), 100)
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
        }
        data = await self.get_json(GAMMA + "/markets", params=params)
        if not isinstance(data, list):
            raise VenueUnavailableError("Polymarket Gamma returned a non-list markets payload")
        markets: list[Market] = []
        for raw in data:
            try:
                market = self.parse_gamma_market(raw)
            except Exception:
                continue
            if query.q and query.q.lower() not in market.title.lower():
                continue
            self._remember(market)
            markets.append(market)
        next_cursor = str(offset + len(data)) if len(data) >= limit else None
        return Page(items=markets[: query.limit], next_cursor=next_cursor)

    @staticmethod
    def _json_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return []
            return parsed if isinstance(parsed, list) else []
        return list(value) if isinstance(value, (list, tuple)) else []

    def parse_gamma_market(self, raw: dict[str, Any]) -> Market:
        market_id = str(raw.get("id") or "").strip()
        if not market_id:
            raise ValueError("Polymarket market has no id")
        labels = [str(x) for x in self._json_list(raw.get("outcomes"))]
        token_ids = [str(x) for x in self._json_list(raw.get("clobTokenIds"))]
        position_ids = [str(x) for x in self._json_list(raw.get("positionIds"))]
        asset_ids = token_ids if len(token_ids) >= 2 else position_ids
        if len(labels) < 2:
            labels = ["Yes", "No"]
        if len(asset_ids) < 2:
            raise VenueUnavailableError("Polymarket market lacks YES/NO token or position ids")
        closed = bool(raw.get("closed"))
        archived = bool(raw.get("archived"))
        accepting = bool(raw.get("acceptingOrders", True))
        active = bool(raw.get("active", True))
        if closed or archived:
            status = MarketStatus.RESOLVED if raw.get("umaResolutionStatus") == "resolved" else MarketStatus.CLOSED
        elif not active or not accepting:
            status = MarketStatus.PAUSED
        else:
            status = MarketStatus.OPEN
        category = str(raw.get("category") or "").strip()
        events = raw.get("events") or []
        event = events[0] if events and isinstance(events[0], dict) else {}
        if not category:
            category = str(event.get("category") or "Other")
        tick_raw = raw.get("orderPriceMinTickSize") or raw.get("minimumTickSize") or "0.01"
        tick = Decimal(str(tick_raw))
        fee_rate = CATEGORY_FEE_RATES.get(category.lower(), CATEGORY_FEE_RATES["other"])
        fee_info = {
            "taker_rate": str(fee_rate),
            "maker_rate": "0",
            "category": category,
            "source": "polymarket category fee schedule 2026-10-30",
            "fees_enabled": raw.get("feesEnabled"),
        }
        outcomes = (
            OutcomeInfo(outcome=Outcome.YES, native_id=asset_ids[0], label=labels[0]),
            OutcomeInfo(outcome=Outcome.NO, native_id=asset_ids[1], label=labels[1]),
        )
        condition_id = str(raw.get("conditionId") or "")
        if condition_id:
            self._condition_index[market_id] = condition_id
        return Market(
            venue=self.id,
            native_id=market_id,
            title=str(raw.get("question") or raw.get("title") or market_id),
            category=category,
            event_id=str(event.get("id") or "") or None,
            event_title=str(event.get("title") or "") or None,
            end_time=parse_iso(raw.get("endDate") or raw.get("endDateIso")),
            status=status,
            tick_size=tick if tick > 0 else Decimal("0.01"),
            neg_risk=bool(raw.get("negRisk", False)),
            outcomes=outcomes,
            fee_info=fee_info,
            url=f"https://polymarket.com/event/{event.get('slug') or raw.get('slug') or market_id}",
            raw_updated_at=parse_iso(raw.get("updatedAt")),
        )

    def _remember(self, market: Market) -> None:
        self._markets[market.native_id] = market
        for info in market.outcomes:
            self._asset_index[info.native_id] = (market.native_id, info.outcome)

    async def get_market(self, ref: MarketRef) -> Market:
        if ref.market_id in self._markets:
            return self._markets[ref.market_id]
        data = await self.get_json(GAMMA + f"/markets/{ref.market_id}")
        market = self.parse_gamma_market(data)
        self._remember(market)
        return market

    async def get_book(self, ref: MarketRef, depth: int = 20) -> BookSnapshot:
        if ref.outcome is None:
            raise ValueError("outcome required")
        market = await self.get_market(ref)
        asset_id = market.outcome(ref.outcome).native_id
        data = await self.get_json(CLOB + "/book", params={"token_id": asset_id})
        bids = self._parse_levels(data.get("bids") or [], reverse=True)
        asks = self._parse_levels(data.get("asks") or [], reverse=False)
        ts_raw = data.get("timestamp")
        timestamp = utcnow()
        if ts_raw:
            try:
                ts = Decimal(str(ts_raw))
                timestamp = datetime.fromtimestamp(float(ts) / (1000 if ts > Decimal("10_000_000_000") else 1), tz=UTC)
            except Exception:
                timestamp = utcnow()
        return BookSnapshot(
            venue=self.id,
            market_id=ref.market_id,
            outcome=ref.outcome,
            bids=tuple(bids[:depth]),
            asks=tuple(asks[:depth]),
            timestamp=timestamp,
        )

    @staticmethod
    def _parse_levels(rows: list[Any], *, reverse: bool) -> list[BookLevel]:
        levels: list[BookLevel] = []
        for row in rows:
            try:
                price = normalize_probability(row["price"], label="polymarket book price")
                size = to_decimal(row["size"])
            except Exception:
                continue
            if size > 0:
                levels.append(BookLevel(price=price, size=size))
        levels.sort(key=lambda level: level.price, reverse=reverse)
        return levels

    async def subscribe_books(self, refs: Sequence[MarketRef]) -> AsyncIterator[BookSnapshot]:
        async for book in self.poll_books(refs, interval=2.0):
            yield book

    # --------------------------------------------------------------- account
    async def _secure(self) -> Any:
        if not polymarket_signing.is_configured():
            raise VenueAuthError("Polymarket signer key absent; authenticated features unavailable")
        wallet = self.settings.polymarket_wallet_address.strip()
        try:
            return await polymarket_signing.secure_client(wallet)
        except Exception as exc:
            self._test_error = str(exc)[:200]
            raise VenueAuthError(f"Polymarket secure client failed: {exc}") from exc

    async def get_balance(self) -> Balance:
        client = await self._secure()
        allowance = await client.get_balance_allowance(asset_type="COLLATERAL")
        available = Decimal(allowance.balance) / Decimal("1000000")
        return Balance(venue=self.id, available_usd=available, total_usd=available, currency="pUSD")

    async def get_positions(self) -> list[Position]:
        client = await self._secure()
        page = await client.list_positions(user=self.settings.polymarket_wallet_address or None).first_page()
        out: list[Position] = []
        for p in page.items:
            market_id = self._asset_index.get(str(p.asset_id), (str(p.condition_id), Outcome.YES))[0]
            outcome_index = int(str(getattr(p, "outcome_index", 0) or 0))
            outcome = Outcome.YES if outcome_index == 0 else Outcome.NO
            out.append(
                Position(
                    venue=self.id,
                    market_id=market_id,
                    outcome=outcome,
                    shares=p.current_size,
                    avg_price=p.avg_price or ZERO,
                    current_price=p.current_price,
                    realized_pnl=p.realized_pnl or ZERO,
                    unrealized_pnl=p.unrealized_pnl or ZERO,
                    market_title=p.title,
                )
            )
        return out

    async def get_open_orders(self, ref: MarketRef | None = None) -> list[VenueOrder]:
        client = await self._secure()
        page = await client.list_open_orders().first_page()
        out: list[VenueOrder] = []
        for order in page.items:
            asset_id = str(order.asset_id)
            mapping = self._asset_index.get(asset_id)
            if ref and mapping and mapping[0] != ref.market_id:
                continue
            outcome = mapping[1] if mapping else None
            out.append(
                VenueOrder(
                    venue=self.id,
                    market_id=mapping[0] if mapping else str(order.condition_id),
                    order_id=str(order.id),
                    outcome=outcome,
                    side=Side(order.side),
                    price=order.price,
                    size=order.original_size,
                    filled=order.size_matched,
                    status=str(order.status),
                    created_at=order.created_at,
                )
            )
        return out

    # ---------------------------------------------------------------- trading
    async def place_order(self, req: OrderRequest) -> OrderResult:
        market = await self.get_market(MarketRef(venue=self.id, market_id=req.market_id))
        asset_id = market.outcome(req.outcome).native_id
        try:
            client = await self._secure()
        except VenueAuthError as exc:
            return OrderResult(
                venue=self.id,
                client_order_id=req.client_order_id,
                status=ExecutionStatus.FAILED,
                requested_size=req.size,
                message=str(exc),
            )
        price = quantize_price(req.price, market.tick_size)
        size = quantize_shares(req.size, 4)
        try:
            accepted = await client.place_limit_order(asset_id=asset_id, price=price, size=size, side=req.side.value)
        except httpx.TimeoutException:
            return self._unknown(req, "timeout")
        except Exception as exc:
            name = type(exc).__name__
            if "Timeout" in name or "Transport" in name or "Connection" in name:
                return self._unknown(req, name)
            return OrderResult(
                venue=self.id,
                client_order_id=req.client_order_id,
                status=ExecutionStatus.FAILED,
                requested_size=req.size,
                message=f"{name}: {str(exc)[:180]}",
            )
        if getattr(accepted, "ok", True) is False:
            return OrderResult(
                venue=self.id,
                client_order_id=req.client_order_id,
                status=ExecutionStatus.FAILED,
                requested_size=req.size,
                message=f"{getattr(accepted, 'code', 'rejected')}: {getattr(accepted, 'message', '')}"[:240],
            )
        status_raw = str(getattr(accepted, "status", "")).lower()
        filled = ZERO
        try:
            making = to_decimal(getattr(accepted, "making_amount", 0))
            taking = to_decimal(getattr(accepted, "taking_amount", 0))
            filled = taking if req.side is Side.BUY else making
        except Exception:
            filled = ZERO
        if status_raw == "matched":
            status = ExecutionStatus.FILLED
            if filled <= 0:
                filled = size
        elif filled > 0:
            status = ExecutionStatus.PARTIAL
        elif status_raw in {"live", "delayed"}:
            status = ExecutionStatus.RESTING
        else:
            status = ExecutionStatus.RESTING
        fee = self.fee(price, filled, req.side, market).fee_usd
        order_id = str(getattr(accepted, "order_id", "")) or None
        if order_id:
            self._asset_index.setdefault(asset_id, (req.market_id, req.outcome))
        return OrderResult(
            venue=self.id,
            client_order_id=req.client_order_id,
            venue_order_id=order_id,
            status=status,
            requested_size=req.size,
            filled_size=min(filled, req.size),
            remaining_size=max(ZERO, req.size - filled),
            average_price=price if filled > 0 else None,
            fee_usd=fee,
            raw_redacted={"status": status_raw, "order_id": order_id},
        )

    def _unknown(self, req: OrderRequest, message: str) -> OrderResult:
        return OrderResult(
            venue=self.id,
            client_order_id=req.client_order_id,
            status=ExecutionStatus.UNKNOWN,
            requested_size=req.size,
            remaining_size=req.size,
            message=message,
            raw_redacted={"uncertain": True},
        )

    async def cancel_order(self, order_id: str) -> CancelResult:
        try:
            client = await self._secure()
            response = await client.cancel_order(order_id=order_id)
            return CancelResult(
                venue=self.id,
                ok=True,
                cancelled=len(getattr(response, "canceled", ()) or ()) or 1,
            )
        except Exception as exc:
            return CancelResult(venue=self.id, ok=False, message=f"{type(exc).__name__}: {str(exc)[:160]}")

    async def cancel_all(self, ref: MarketRef | None = None) -> CancelResult:
        try:
            client = await self._secure()
            if ref is not None:
                market = await self.get_market(ref)
                asset_id = market.outcome(ref.outcome or Outcome.YES).native_id
                response = await client.cancel_market_orders(asset_id=asset_id)
            else:
                response = await client.cancel_all()
            return CancelResult(
                venue=self.id,
                ok=True,
                cancelled=len(getattr(response, "canceled", ()) or ()),
            )
        except Exception as exc:
            return CancelResult(venue=self.id, ok=False, message=f"{type(exc).__name__}: {str(exc)[:160]}")

    # ------------------------------------------------------------------- fee
    def fee(self, price: Decimal, size: Decimal, side: Side, market: Market) -> FeeQuote:
        del side  # every agent order is a taker; makers pay zero
        rate = Decimal(str(market.fee_info.get("taker_rate", "0.05")))
        raw = size * rate * price * (ONE - price)
        fee = raw.quantize(Decimal("0.00001"))
        return FeeQuote(fee_usd=fee, rate=rate, source="polymarket taker fee C*rate*p*(1-p)")

    # ---------------------------------------------------------------- doctor
    async def doctor(self) -> DoctorReport:
        checks: list[DoctorCheck] = []
        start = time.perf_counter()
        try:
            page = await self.list_markets(MarketQuery(limit=1))
            if not page.items:
                raise VenueUnavailableError("Gamma returned no markets")
            market = page.items[0]
            check_ref = MarketRef(venue=self.id, market_id=market.native_id, outcome=Outcome.YES)
            book = await self.get_book(check_ref, depth=5)
            if not book.asks and not book.bids:
                raise VenueUnavailableError("CLOB book empty")
            checks.append(
                DoctorCheck(
                    name="public_read",
                    status=DoctorStatus.OK,
                    detail=f"{market.title[:60]}",
                    latency_ms=int((time.perf_counter() - start) * 1000),
                )
            )
            checks.append(DoctorCheck(name="book_read", status=DoctorStatus.OK, detail=book.key))
        except Exception as exc:
            checks.append(DoctorCheck(name="public_read", status=DoctorStatus.UNAVAILABLE, detail=str(exc)[:200]))
            return DoctorReport(venue=self.id, status=DoctorStatus.UNAVAILABLE, checks=tuple(checks), reason=str(exc))

        auth_ok = False
        if not self.settings.credentials_present(self.id):
            checks.append(
                DoctorCheck(
                    name="authenticated_read",
                    status=DoctorStatus.SKIPPED,
                    detail="no signer key; public reads and PAPER work",
                )
            )
        else:
            try:
                balance = await self.get_balance()
                auth_ok = True
                checks.append(
                    DoctorCheck(
                        name="authenticated_read",
                        status=DoctorStatus.OK,
                        detail=f"balance {balance.available_usd:.2f} pUSD",
                    )
                )
            except Exception as exc:
                checks.append(
                    DoctorCheck(
                        name="authenticated_read",
                        status=DoctorStatus.UNAVAILABLE,
                        detail=str(exc)[:200],
                    )
                )
        if self.test_orders:
            if not auth_ok:
                checks.append(DoctorCheck(name="order_roundtrip", status=DoctorStatus.SKIPPED, detail="auth required"))
            else:
                checks.append(await self._doctor_order())
        else:
            checks.append(
                DoctorCheck(
                    name="order_roundtrip",
                    status=DoctorStatus.SKIPPED,
                    detail="opt-in only; pass --i-accept-real-money",
                )
            )
        status = (
            DoctorStatus.OK
            if all(c.status in {DoctorStatus.OK, DoctorStatus.SKIPPED} for c in checks)
            else DoctorStatus.DEGRADED
        )
        return DoctorReport(
            venue=self.id,
            status=status,
            checks=tuple(checks),
            reason="" if auth_ok else "signer absent or auth failed; PAPER only",
        )

    async def _doctor_order(self) -> DoctorCheck:
        try:
            market = (await self.list_markets(MarketQuery(limit=1))).items[0]
            price = max(market.tick_size, Decimal("0.01"))
            request = OrderRequest(
                venue=self.id,
                market_id=market.native_id,
                outcome=Outcome.YES,
                side=Side.BUY,
                price=price,
                size=Decimal("5"),
                client_order_id="doctor-" + uuid.uuid4().hex,
            )
            result = await self.place_order(request)
            if result.venue_order_id:
                await self.cancel_order(result.venue_order_id)
            ok = result.status not in {ExecutionStatus.FAILED, ExecutionStatus.UNKNOWN, ExecutionStatus.ERROR}
            return DoctorCheck(
                name="order_roundtrip",
                status=DoctorStatus.OK if ok else DoctorStatus.DEGRADED,
                detail=f"place={result.status.value} cancel=attempted",
            )
        except Exception as exc:
            return DoctorCheck(name="order_roundtrip", status=DoctorStatus.DEGRADED, detail=str(exc)[:200])

    async def close(self) -> None:
        await super().close()
        await polymarket_signing.close_client()
