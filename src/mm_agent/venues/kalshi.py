"""Kalshi adapter. Public reads plus signed portfolio/order endpoints."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Sequence
from decimal import ROUND_CEILING, Decimal
from typing import Any
from urllib.parse import urlparse

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
from .base import HttpVenue, MarketQuery, Page, VenueAuthError, VenueHTTPError, VenueUnavailableError
from .kalshi_signing import KalshiSigner

ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")


class KalshiVenue(HttpVenue):
    id = VenueId.KALSHI
    caps = VenueCaps(
        realtime=True,
        sandbox=True,
        neg_risk=False,
        min_order_shares=Decimal("1"),
        min_notional_usd=Decimal("0.01"),
        default_tick=CENT,
    )

    def __init__(self, settings: Settings, *, base_url: str | None = None) -> None:
        super().__init__(settings)
        self.base_url = (base_url or settings.kalshi_base_url).rstrip("/")
        self._signer: KalshiSigner | None = None
        self._signer_error: str | None = None
        self._order_markets: dict[str, str] = {}
        self.test_orders = False

    @property
    def display_name(self) -> str:
        return "Kalshi"

    def _ensure_signer(self) -> KalshiSigner:
        if self._signer is None:
            try:
                self._signer = KalshiSigner.from_env(self.settings.kalshi_api_key_id)
            except Exception as exc:
                self._signer_error = str(exc)
                raise VenueAuthError(f"Kalshi credentials unavailable: {exc}") from exc
        return self._signer

    async def _signed(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None) -> Any:
        signer = self._ensure_signer()
        url = self.base_url + path
        full_path = urlparse(url).path
        headers = signer.headers(method, full_path)
        response = await self.client.request(method, url, params=params, json=json, headers=headers)
        if response.status_code >= 400:
            raise VenueHTTPError(response.status_code, response.text[:400], path)
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    # ------------------------------------------------------------- discovery
    async def list_markets(self, query: MarketQuery) -> Page[Market]:
        # The raw /markets feed is dominated by multivariate combo markets, so
        # discovery walks events with nested markets and filters those combos.
        params: dict[str, Any] = {
            "limit": min(max(query.limit, 1), 100),
            "with_nested_markets": "true",
        }
        if not query.include_closed:
            params["status"] = "open"
        if query.cursor:
            params["cursor"] = query.cursor
        data = await self.get_json(self.base_url + "/events", params=params)
        markets: list[Market] = []
        for event in data.get("events", []):
            for raw in event.get("markets") or []:
                enriched = dict(raw)
                enriched.setdefault("event_ticker", event.get("event_ticker"))
                enriched.setdefault("event_title", event.get("title"))
                enriched.setdefault("category", event.get("category"))
                try:
                    market = self._parse_market(enriched)
                except Exception:
                    continue
                if market is None:
                    continue
                if query.q and query.q.lower() not in market.title.lower():
                    continue
                if query.category and query.category.lower() not in market.category.lower():
                    continue
                markets.append(market)
                if len(markets) >= query.limit:
                    break
            if len(markets) >= query.limit:
                break
        next_cursor = data.get("cursor")
        return Page(items=markets[: query.limit], next_cursor=next_cursor)

    def _parse_market(self, raw: dict[str, Any]) -> Market | None:
        if raw.get("market_type") not in {None, "binary"}:
            return None
        # Multi-leg multivariate ("combo") markets are not supported in v1.
        if raw.get("mve_collection_ticker"):
            return None
        ticker = raw.get("ticker")
        if not ticker:
            return None
        status_raw = str(raw.get("status", "")).lower()
        status = {
            "active": MarketStatus.OPEN,
            "open": MarketStatus.OPEN,
            "initialized": MarketStatus.OPEN,
            "closed": MarketStatus.CLOSED,
            "determined": MarketStatus.CLOSED,
            "settled": MarketStatus.RESOLVED,
            "finalized": MarketStatus.RESOLVED,
        }.get(status_raw, MarketStatus.UNKNOWN)
        tick = self._central_tick(raw)
        title = raw.get("title") or raw.get("yes_sub_title") or ticker
        event_ticker = raw.get("event_ticker")
        outcomes = (
            OutcomeInfo(outcome=Outcome.YES, native_id=ticker, label="Yes"),
            OutcomeInfo(outcome=Outcome.NO, native_id=ticker, label="No"),
        )
        end = parse_iso(raw.get("close_time") or raw.get("expiration_time"))
        return Market(
            venue=self.id,
            native_id=ticker,
            title=title,
            category=str(raw.get("category") or "Other"),
            event_id=event_ticker,
            event_title=str(raw.get("event_title") or event_ticker or "") or None,
            end_time=end,
            status=status,
            tick_size=tick,
            outcomes=outcomes,
            fee_info={
                "taker_rate": "0.07",
                "maker_rate": "0",
                "model": "kalshi_standard",
                "price_ranges": raw.get("price_ranges") or [],
            },
            url=f"https://kalshi.com/markets/{ticker.lower()}",
            raw_updated_at=parse_iso(raw.get("updated_time")),
        )

    @staticmethod
    def _central_tick(raw: dict[str, Any]) -> Decimal:
        ranges = raw.get("price_ranges") or []
        for band in ranges:
            try:
                start = Decimal(str(band.get("start")))
                end = Decimal(str(band.get("end")))
            except Exception:
                continue
            if start <= Decimal("0.5") <= end:
                return Decimal(str(band.get("step")))
        ranges = [Decimal(str(b.get("step"))) for b in ranges if b.get("step")]
        return min(ranges) if ranges else CENT

    async def get_market(self, ref: MarketRef) -> Market:
        data = await self.get_json(self.base_url + f"/markets/{ref.market_id}")
        market = self._parse_market(data.get("market", {}))
        if market is None:
            raise VenueUnavailableError(f"Kalshi market {ref.market_id} is not a supported binary market")
        return market

    async def get_book(self, ref: MarketRef, depth: int = 20) -> BookSnapshot:
        if ref.outcome is None:
            raise ValueError("outcome required")
        data = await self.get_json(self.base_url + f"/markets/{ref.market_id}/orderbook", params={"depth": depth})
        book = data.get("orderbook_fp") or data.get("orderbook") or {}
        yes_raw = book.get("yes_dollars") or book.get("yes") or []
        no_raw = book.get("no_dollars") or book.get("no") or []
        yes_bids = self._levels(yes_raw)
        no_bids = self._levels(no_raw)
        if ref.outcome is Outcome.YES:
            bids = yes_bids
            asks = tuple(
                sorted(
                    (BookLevel(price=ONE - level.price, size=level.size) for level in no_bids),
                    key=lambda level: level.price,
                )
            )[:depth]
        else:
            bids = no_bids
            asks = tuple(
                sorted(
                    (BookLevel(price=ONE - level.price, size=level.size) for level in yes_bids),
                    key=lambda level: level.price,
                )
            )[:depth]
        return BookSnapshot(
            venue=self.id,
            market_id=ref.market_id,
            outcome=ref.outcome,
            bids=tuple(bids[:depth]),
            asks=asks,
            timestamp=utcnow(),
        )

    @staticmethod
    def _levels(rows: list[Any]) -> tuple[BookLevel, ...]:
        levels: list[BookLevel] = []
        for row in rows:
            try:
                price, size = row[0], row[1]
            except (IndexError, TypeError):
                continue
            p = normalize_probability(price, label="kalshi book price")
            s = to_decimal(size)
            if s > 0:
                levels.append(BookLevel(price=p, size=s))
        levels.sort(key=lambda level: level.price, reverse=True)
        return tuple(levels)

    async def subscribe_books(self, refs: Sequence[MarketRef]) -> AsyncIterator[BookSnapshot]:
        async for book in self.poll_books(refs):
            yield book

    # --------------------------------------------------------------- account
    async def get_balance(self) -> Balance:
        data = await self._signed("GET", "/portfolio/balance")
        available = Decimal(str(data.get("balance_dollars", "0")))
        total = available + Decimal(str(data.get("portfolio_value", 0))) / Decimal("100")
        return Balance(venue=self.id, available_usd=available, total_usd=total)

    async def get_positions(self) -> list[Position]:
        data = await self._signed("GET", "/portfolio/positions", params={"limit": 200})
        rows = data.get("market_positions") or data.get("positions") or []
        out: list[Position] = []
        for row in rows:
            ticker = row.get("ticker") or row.get("market_ticker")
            if not ticker:
                continue
            contracts = to_decimal(
                row.get("position_fp")
                or row.get("position")
                or row.get("net_position")
                or row.get("market_exposure")
                or "0"
            )
            if contracts == 0:
                continue
            exposure = to_decimal(row.get("market_exposure_dollars") or row.get("market_exposure") or 0)
            if row.get("market_exposure_dollars") is None and exposure > 0:
                exposure = exposure / Decimal("100")
            avg = abs(exposure / contracts) if contracts else ZERO
            outcome = Outcome.YES if contracts > 0 else Outcome.NO
            out.append(
                Position(
                    venue=self.id,
                    market_id=ticker,
                    outcome=outcome,
                    shares=abs(contracts),
                    avg_price=avg,
                    market_title=row.get("title"),
                )
            )
        return out

    async def get_open_orders(self, ref: MarketRef | None = None) -> list[VenueOrder]:
        params: dict[str, Any] = {"limit": 200}
        if ref:
            params["ticker"] = ref.market_id
        data = await self._signed("GET", "/portfolio/orders", params=params)
        rows = data.get("orders", [])
        out: list[VenueOrder] = []
        for row in rows:
            ticker = row.get("ticker") or row.get("market_ticker")
            if not ticker:
                continue
            side_raw = str(row.get("side", "buy")).lower()
            action_raw = str(row.get("action", "buy")).lower()
            # V2 is YES-book bid/ask; older responses use yes/no + buy/sell.
            if side_raw in {"bid", "ask"}:
                outcome = Outcome.YES
                side = Side.BUY if side_raw == "bid" else Side.SELL
                price = to_decimal(row.get("yes_price_dollars") or row.get("price_dollars") or "0")
            else:
                outcome = Outcome.YES if side_raw == "yes" else Outcome.NO
                side = Side.BUY if action_raw == "buy" else Side.SELL
                price = to_decimal(row.get("price_dollars") or row.get("price") or "0")
                if price > 1:
                    price = price / Decimal("100")
            size = to_decimal(row.get("initial_count_fp") or row.get("count_fp") or row.get("count") or 0)
            filled = to_decimal(row.get("fill_count_fp") or row.get("filled_count_fp") or row.get("filled_count") or 0)
            order_id = str(row.get("order_id") or row.get("id") or "")
            if not order_id:
                continue
            self._order_markets[order_id] = ticker
            out.append(
                VenueOrder(
                    venue=self.id,
                    market_id=ticker,
                    order_id=order_id,
                    client_order_id=row.get("client_order_id"),
                    outcome=outcome,
                    side=side,
                    price=price,
                    size=size,
                    filled=filled,
                    status=str(row.get("status", "resting")),
                    reduce_only=bool(row.get("reduce_only", False)),
                    created_at=parse_iso(row.get("created_time")),
                )
            )
        return out

    # ---------------------------------------------------------------- trading
    async def place_order(self, req: OrderRequest) -> OrderResult:
        self._ensure_signer()
        market = await self.get_market(MarketRef(venue=self.id, market_id=req.market_id))
        venue_outcome = req.outcome
        venue_side = "bid" if req.side is Side.BUY else "ask"
        venue_price = req.price
        if venue_outcome is Outcome.NO:
            # V2 orders quote the YES book only: buying NO == selling YES at 1-p.
            venue_price = ONE - req.price
            venue_side = "ask" if req.side is Side.BUY else "bid"
        venue_price = quantize_price(venue_price, market.tick_size)
        if not (ZERO < venue_price < ONE):
            return OrderResult(
                venue=self.id,
                client_order_id=req.client_order_id,
                status=ExecutionStatus.FAILED,
                requested_size=req.size,
                message="price out of range after tick alignment",
            )
        tif = {
            "GTC": "good_till_canceled",
            "FAK": "immediate_or_cancel",
            "IOC": "immediate_or_cancel",
            "FOK": "fill_or_kill",
        }[req.order_type]
        payload: dict[str, Any] = {
            "ticker": req.market_id,
            "side": venue_side,
            "count": f"{quantize_shares(req.size, 2):.2f}",
            "price": f"{venue_price:.4f}",
            "time_in_force": tif,
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": req.client_order_id,
        }
        if req.reduce_only:
            if tif != "immediate_or_cancel":
                return OrderResult(
                    venue=self.id,
                    client_order_id=req.client_order_id,
                    status=ExecutionStatus.FAILED,
                    requested_size=req.size,
                    message="Kalshi reduce-only requires IOC",
                )
            payload["reduce_only"] = True
        try:
            response = await self._signed("POST", "/portfolio/events/orders", json=payload)
        except httpx.TimeoutException:
            return self._unknown(req, "timeout")
        except httpx.NetworkError as exc:
            return self._unknown(req, f"network error: {type(exc).__name__}")
        except VenueHTTPError as exc:
            if exc.status_code == 409:
                return OrderResult(
                    venue=self.id,
                    client_order_id=req.client_order_id,
                    status=ExecutionStatus.UNKNOWN,
                    requested_size=req.size,
                    message="duplicate client_order_id; reconcile, never resend",
                    raw_redacted={"http_status": 409},
                )
            if 400 <= exc.status_code < 500:
                return OrderResult(
                    venue=self.id,
                    client_order_id=req.client_order_id,
                    status=ExecutionStatus.FAILED,
                    requested_size=req.size,
                    message=f"venue rejected ({exc.status_code}): {exc.body[:200]}",
                    raw_redacted={"http_status": exc.status_code},
                )
            return self._unknown(req, f"server error {exc.status_code}")
        except Exception as exc:
            return self._unknown(req, f"unexpected {type(exc).__name__}")

        order_id = response.get("order_id")
        fill_count = to_decimal(response.get("fill_count") or 0)
        remaining = to_decimal(response.get("remaining_count") or 0)
        avg = response.get("average_fill_price")
        if fill_count >= req.size:
            status = ExecutionStatus.FILLED
        elif fill_count > 0:
            status = ExecutionStatus.PARTIAL
        elif remaining > 0:
            status = ExecutionStatus.RESTING
        else:
            status = ExecutionStatus.NOT_FILLED
        if order_id:
            self._order_markets[str(order_id)] = req.market_id
        return OrderResult(
            venue=self.id,
            client_order_id=req.client_order_id,
            venue_order_id=str(order_id) if order_id else None,
            status=status,
            requested_size=req.size,
            filled_size=fill_count,
            remaining_size=remaining,
            average_price=normalize_probability(avg) if avg else None,
            raw_redacted={"order_id": order_id, "fill_count": str(fill_count)},
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
        self._ensure_signer()
        ticker = self._order_markets.get(order_id)
        if not ticker:
            for order in await self.get_open_orders():
                if order.order_id == order_id:
                    ticker = order.market_id
                    break
        params = {"market_ticker": ticker} if ticker else {}
        try:
            await self._signed("DELETE", f"/portfolio/events/orders/{order_id}", params=params)
            return CancelResult(venue=self.id, ok=True, cancelled=1)
        except VenueHTTPError as exc:
            return CancelResult(venue=self.id, ok=False, message=f"HTTP {exc.status_code}: {exc.body[:160]}")
        except Exception as exc:
            return CancelResult(venue=self.id, ok=False, message=f"{type(exc).__name__}")

    async def cancel_all(self, ref: MarketRef | None = None) -> CancelResult:
        self._ensure_signer()
        try:
            if ref is None:
                await self._signed("DELETE", "/portfolio/events/orders")
                return CancelResult(venue=self.id, ok=True, cancelled=-1, message="cancel all acknowledged")
            orders = await self.get_open_orders(ref)
            cancelled = 0
            for order in orders:
                result = await self.cancel_order(order.order_id)
                if result.ok:
                    cancelled += 1
            return CancelResult(venue=self.id, ok=True, cancelled=cancelled)
        except Exception as exc:
            return CancelResult(venue=self.id, ok=False, message=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------- fee
    def fee(self, price: Decimal, size: Decimal, side: Side, market: Market) -> FeeQuote:
        del market, side  # the agent only sends crossing/taker orders
        rate = Decimal("0.07")
        raw = rate * size * price * (ONE - price)
        if raw <= 0:
            return FeeQuote(fee_usd=ZERO, rate=rate, source="kalshi-standard")
        # Kalshi rounds the fee up to the next cent.
        fee = raw.quantize(CENT, rounding=ROUND_CEILING)
        return FeeQuote(fee_usd=fee, rate=rate, source="kalshi-standard ceil-cent")

    # ---------------------------------------------------------------- doctor
    async def doctor(self) -> DoctorReport:
        checks: list[DoctorCheck] = []
        start = time.perf_counter()
        try:
            data = await self.get_json(self.base_url + "/exchange/status")
            if not data.get("trading_active", False):
                checks.append(DoctorCheck(name="public_read", status=DoctorStatus.DEGRADED, detail="trading inactive"))
            else:
                checks.append(DoctorCheck(name="public_read", status=DoctorStatus.OK, detail="exchange trading"))
            markets = await self.list_markets(MarketQuery(limit=1))
            checks.append(
                DoctorCheck(
                    name="market_read",
                    status=DoctorStatus.OK if markets.items else DoctorStatus.DEGRADED,
                    detail=f"{len(markets.items)} market(s)",
                    latency_ms=int((time.perf_counter() - start) * 1000),
                )
            )
        except Exception as exc:
            checks.append(DoctorCheck(name="public_read", status=DoctorStatus.UNAVAILABLE, detail=str(exc)[:200]))
            return DoctorReport(venue=self.id, status=DoctorStatus.UNAVAILABLE, checks=tuple(checks), reason=str(exc))

        auth_ok = False
        if not self.settings.credentials_present(self.id):
            checks.append(
                DoctorCheck(
                    name="authenticated_read",
                    status=DoctorStatus.SKIPPED,
                    detail="no Kalshi credentials; public reads and PAPER work",
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
                        detail=f"balance ${balance.available_usd:.2f}",
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
        status = DoctorStatus.OK if all(c.status is DoctorStatus.OK for c in checks) else DoctorStatus.DEGRADED
        reason = "" if auth_ok else "credentials absent or auth failed; PAPER only"
        return DoctorReport(venue=self.id, status=status, checks=tuple(checks), reason=reason)

    async def _doctor_order(self) -> DoctorCheck:
        """Tiny opt-in order test. Never called during the build or in tests."""

        try:
            markets = await self.list_markets(MarketQuery(limit=5))
            if not markets.items:
                return DoctorCheck(name="order_roundtrip", status=DoctorStatus.SKIPPED, detail="no market")
            market = markets.items[0]
            request = OrderRequest(
                venue=self.id,
                market_id=market.native_id,
                outcome=Outcome.YES,
                side=Side.BUY,
                price=market.tick_size,
                size=Decimal("1"),
                client_order_id="doctor-" + uuid.uuid4().hex,
                order_type="GTC",
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
