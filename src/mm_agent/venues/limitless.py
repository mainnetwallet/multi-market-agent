"""Limitless Exchange adapter (Base / USDC).

Public market data is direct REST. Trading uses HMAC request auth plus EIP-712
order signatures from the isolated signing module.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
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
    quantize_shares,
    to_decimal,
    utcnow,
)
from . import limitless_signing
from .base import HttpVenue, MarketQuery, Page, VenueAuthError, VenueHTTPError, VenueUnavailableError

ZERO = Decimal("0")
ONE = Decimal("1")


class LimitlessVenue(HttpVenue):
    id = VenueId.LIMITLESS
    caps = VenueCaps(
        realtime=True,
        sandbox=False,
        neg_risk=True,
        min_order_shares=Decimal("1"),
        min_notional_usd=Decimal("0.01"),
        default_tick=Decimal("0.001"),
    )

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.base_url = settings.limitless_api_url.rstrip("/")
        self._markets: dict[str, Market] = {}
        self._raw: dict[str, dict[str, Any]] = {}
        self._profile: dict[str, Any] | None = None
        self.test_orders = False

    @property
    def display_name(self) -> str:
        return "Limitless"

    # ------------------------------------------------------------- discovery
    async def list_markets(self, query: MarketQuery) -> Page[Market]:
        page_no = int(query.cursor) if query.cursor and query.cursor.isdigit() else 1
        # Live API (2026-10-30) rejects limit > 25 on /markets/active.
        limit = min(max(query.limit, 1), 25)
        params = {"limit": limit, "page": page_no}
        data = await self.get_json(self.base_url + "/markets/active", params=params)
        rows = data.get("data") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise VenueUnavailableError("Limitless returned an unexpected markets payload")
        markets: list[Market] = []
        for raw in rows:
            try:
                market = self.parse_market(raw)
            except Exception:
                continue
            if query.q and query.q.lower() not in market.title.lower():
                continue
            self._remember(market, raw)
            markets.append(market)
        next_cursor = str(page_no + 1) if len(rows) >= limit else None
        return Page(items=markets[: query.limit], next_cursor=next_cursor)

    def parse_market(self, raw: dict[str, Any]) -> Market:
        slug = str(raw.get("slug") or "").strip()
        if not slug:
            raise ValueError("Limitless market has no slug")
        if str(raw.get("tradeType", "clob")).lower() != "clob":
            raise VenueUnavailableError("not a CLOB market")
        tokens = raw.get("tokens") or {}
        yes_id = str(tokens.get("yes") or "")
        no_id = str(tokens.get("no") or "")
        if not yes_id or not no_id:
            raise VenueUnavailableError("Limitless market lacks YES/NO token ids")
        status_raw = str(raw.get("status", "")).upper()
        if raw.get("expired"):
            status = MarketStatus.CLOSED
        elif status_raw in {"CREATED", "FUNDED", "OPEN"}:
            status = MarketStatus.OPEN
        elif status_raw in {"LOCKED", "PAUSED"}:
            status = MarketStatus.PAUSED
        elif status_raw in {"RESOLVED", "SETTLED"}:
            status = MarketStatus.RESOLVED
        else:
            status = MarketStatus.UNKNOWN
        end_ms = raw.get("expirationTimestamp")
        end_time = (
            datetime.fromtimestamp(float(end_ms) / 1000, tz=UTC)
            if isinstance(end_ms, (int, float)) and end_ms > 0
            else parse_iso(raw.get("expirationDate"))
        )
        categories = raw.get("categories") or []
        metadata = raw.get("metadata") or {}
        fee_enabled = bool(metadata.get("fee"))
        fee_info = {
            "taker_rate": "0.02" if fee_enabled else "0",
            "maker_rate": "0",
            "source": "conservative Limitless estimate; confirm venue fee schedule",
            "fee_enabled": fee_enabled,
            "fee_module_type": (raw.get("venue") or {}).get("feeModuleType"),
        }
        return Market(
            venue=self.id,
            native_id=slug,
            title=str(raw.get("title") or raw.get("proxyTitle") or slug),
            category=str(categories[0]) if categories else "Other",
            event_id=str(raw.get("groupId") or "") or None,
            end_time=end_time,
            status=status,
            tick_size=Decimal("0.001"),
            neg_risk=bool(raw.get("negRiskRequestId")),
            outcomes=(
                OutcomeInfo(outcome=Outcome.YES, native_id=yes_id, label="Yes"),
                OutcomeInfo(outcome=Outcome.NO, native_id=no_id, label="No"),
            ),
            fee_info=fee_info,
            url=f"https://limitless.exchange/markets/{slug}",
            raw_updated_at=parse_iso(raw.get("updatedAt")),
        )

    def _remember(self, market: Market, raw: dict[str, Any]) -> None:
        self._markets[market.native_id] = market
        self._raw[market.native_id] = raw

    async def get_market(self, ref: MarketRef) -> Market:
        if ref.market_id in self._markets:
            return self._markets[ref.market_id]
        data = await self.get_json(self.base_url + f"/markets/{ref.market_id}")
        raw = data.get("data") if isinstance(data, dict) and "data" in data else data
        if not isinstance(raw, dict):
            raise VenueUnavailableError("Limitless market payload malformed")
        market = self.parse_market(raw)
        self._remember(market, raw)
        return market

    async def get_book(self, ref: MarketRef, depth: int = 20) -> BookSnapshot:
        if ref.outcome is None:
            raise ValueError("outcome required")
        data = await self.get_json(self.base_url + f"/markets/{ref.market_id}/orderbook")
        yes_bids = self._levels(data.get("bids") or [], reverse=True)
        yes_asks = self._levels(data.get("asks") or [], reverse=False)
        if ref.outcome is Outcome.YES:
            bids, asks = yes_bids, yes_asks
        else:
            bids = sorted(
                (BookLevel(price=ONE - level.price, size=level.size) for level in yes_asks),
                key=lambda level: level.price,
                reverse=True,
            )
            asks = sorted(
                (BookLevel(price=ONE - level.price, size=level.size) for level in yes_bids),
                key=lambda level: level.price,
            )
        return BookSnapshot(
            venue=self.id,
            market_id=ref.market_id,
            outcome=ref.outcome,
            bids=tuple(bids[:depth]),
            asks=tuple(asks[:depth]),
            timestamp=utcnow(),
        )

    @staticmethod
    def _levels(rows: list[dict[str, Any]], *, reverse: bool) -> list[BookLevel]:
        levels: list[BookLevel] = []
        for row in rows:
            try:
                price = normalize_probability(row["price"], label="limitless price")
                size = to_decimal(row["size"]) / Decimal("1000000")
            except Exception:
                continue
            if size > 0:
                levels.append(BookLevel(price=price, size=size))
        levels.sort(key=lambda level: level.price, reverse=reverse)
        return levels

    async def subscribe_books(self, refs: Sequence[MarketRef]) -> AsyncIterator[BookSnapshot]:
        async for book in self.poll_books(refs):
            yield book

    # ------------------------------------------------------------------ auth
    def _credentials(self) -> None:
        if not self.settings.credentials_present(self.id):
            raise VenueAuthError("Limitless HMAC token or wallet key absent")

    async def _signed_json(self, method: str, path_with_query: str, body_obj: Any | None = None) -> Any:
        self._credentials()
        body = "" if body_obj is None else json.dumps(body_obj, separators=(",", ":"))
        headers = limitless_signing.auth_headers(method, path_with_query, body)
        headers["Content-Type"] = "application/json"
        response = await self.client.request(
            method,
            self.base_url + path_with_query,
            content=body if body else None,
            headers=headers,
        )
        if response.status_code >= 400:
            raise VenueHTTPError(response.status_code, response.text[:400], path_with_query)
        if not response.content:
            return {}
        return response.json()

    async def _profile_data(self) -> dict[str, Any]:
        if self._profile is not None:
            return self._profile
        self._credentials()
        path = "/profiles/me"
        data = await self._signed_json("GET", path)
        if not isinstance(data, dict):
            raise VenueUnavailableError("Limitless profile payload malformed")
        self._profile = data
        return data

    async def get_balance(self) -> Balance:
        self._credentials()
        wallet = limitless_signing.wallet_address()
        data = "0x70a08231" + "0" * 24 + wallet.lower().removeprefix("0x")
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": self.settings.limitless_usdc_address, "data": data}, "latest"],
        }
        response = await self.client.post(self.settings.base_rpc_url, json=payload)
        response.raise_for_status()
        result = response.json().get("result", "0x0")
        raw = int(result, 16) if isinstance(result, str) and result.startswith("0x") else 0
        available = Decimal(raw) / Decimal("1000000")
        return Balance(venue=self.id, available_usd=available, total_usd=available, currency="USDC")

    async def get_positions(self) -> list[Position]:
        data = await self._signed_json("GET", "/portfolio/positions")
        rows = data.get("clob") or [] if isinstance(data, dict) else []
        out: list[Position] = []
        for row in rows:
            market_raw = row.get("market") or {}
            slug = str(market_raw.get("slug") or "")
            title = market_raw.get("title")
            positions = row.get("positions") or {}
            balances = row.get("tokensBalance") or {}
            for outcome_key in ("yes", "no"):
                pos = positions.get(outcome_key) or {}
                raw_balance = balances.get(outcome_key)
                shares = ZERO
                if raw_balance is not None:
                    try:
                        shares = Decimal(str(raw_balance)) / Decimal("1000000")
                    except InvalidOperation:
                        shares = ZERO
                if shares <= 0:
                    cost = to_decimal(pos.get("cost") or 0)
                    fill = to_decimal(pos.get("fillPrice") or 0)
                    shares = cost / fill if fill > 0 else ZERO
                if shares <= 0:
                    continue
                cost = to_decimal(pos.get("cost") or 0)
                fill = to_decimal(pos.get("fillPrice") or 0)
                avg = fill / Decimal("1000000") if fill > 1 else fill
                if avg <= 0 and shares > 0:
                    avg = (cost / Decimal("1000000")) / shares if cost > 0 else ZERO
                out.append(
                    Position(
                        venue=self.id,
                        market_id=slug,
                        outcome=Outcome.YES if outcome_key == "yes" else Outcome.NO,
                        shares=shares,
                        avg_price=avg,
                        realized_pnl=to_decimal(pos.get("realisedPnl") or 0) / Decimal("1000000"),
                        unrealized_pnl=to_decimal(pos.get("unrealizedPnl") or 0) / Decimal("1000000"),
                        market_title=title,
                    )
                )
        return out

    async def get_open_orders(self, ref: MarketRef | None = None) -> list[VenueOrder]:
        if ref is None:
            return []
        path = f"/markets/{ref.market_id}/user-orders?statuses=LIVE&limit=200"
        data = await self._signed_json("GET", path)
        if not isinstance(data, list):
            return []
        out: list[VenueOrder] = []
        for row in data:
            size = to_decimal(row.get("originalSize") or 0) / Decimal("1000000")
            remaining = to_decimal(row.get("remainingSize") or 0) / Decimal("1000000")
            out.append(
                VenueOrder(
                    venue=self.id,
                    market_id=ref.market_id,
                    order_id=str(row.get("id") or ""),
                    client_order_id=row.get("clientOrderId"),
                    side=Side(str(row.get("side", "BUY")).upper()),
                    price=to_decimal(row.get("price") or 0),
                    size=size,
                    filled=size - remaining,
                    status=str(row.get("status", "LIVE")),
                    created_at=parse_iso(row.get("createdAt")),
                )
            )
        return out

    # --------------------------------------------------------------- trading
    async def place_order(self, req: OrderRequest) -> OrderResult:
        try:
            self._credentials()
            market = await self.get_market(MarketRef(venue=self.id, market_id=req.market_id))
            raw = self._raw.get(market.native_id, {})
            venue = raw.get("venue") or {}
            verifying = str(venue.get("exchange") or "")
            if not verifying:
                raise VenueUnavailableError("Limitless market has no venue exchange address")
            token_id = market.outcome(req.outcome).native_id
            size = quantize_shares(req.size, 2)
            price = req.price.quantize(Decimal("0.001"))
            maker_units = int(price * size * Decimal("1000000"))
            taker_units = int(size * Decimal("1000000"))
            if maker_units < 100 or taker_units < 1:
                return OrderResult(
                    venue=self.id,
                    client_order_id=req.client_order_id,
                    status=ExecutionStatus.FAILED,
                    requested_size=req.size,
                    message="order below Limitless dust floor",
                )
            fee_rate_bps = 0
            if str(venue.get("feeModuleType", "")) != "COLLATERAL" and (raw.get("metadata") or {}).get("fee"):
                profile = await self._profile_data()
                fee_rate_bps = int(profile.get("effectiveFeeRateBps") or 0)
            maker = limitless_signing.wallet_address()
            salt = limitless_signing.salt()
            signature = limitless_signing.sign_order(
                salt=salt,
                maker=maker,
                token_id=token_id,
                maker_amount=maker_units,
                taker_amount=taker_units,
                fee_rate_bps=fee_rate_bps,
                side=0 if req.side is Side.BUY else 1,
                verifying_contract=verifying,
            )
            order = {
                "salt": salt,
                "maker": maker,
                "signer": maker,
                "taker": limitless_signing.ZERO_ADDRESS,
                "tokenId": token_id,
                "makerAmount": maker_units,
                "takerAmount": taker_units,
                "expiration": "0",
                "nonce": 0,
                "feeRateBps": fee_rate_bps,
                "side": 0 if req.side is Side.BUY else 1,
                "signature": signature,
                "signatureType": 0,
                "price": float(price),
            }
            owner_id = self.settings.lmts_owner_id
            if owner_id <= 0:
                profile = await self._profile_data()
                owner_id = int(profile.get("id") or 0)
            body = {
                "order": order,
                "ownerId": owner_id,
                "orderType": "FAK" if req.order_type in {"FAK", "IOC"} else req.order_type,
                "marketSlug": market.native_id,
                "clientOrderId": req.client_order_id,
                "stpPolicy": "cancel_taker",
            }
        except VenueAuthError as exc:
            return OrderResult(
                venue=self.id,
                client_order_id=req.client_order_id,
                status=ExecutionStatus.FAILED,
                requested_size=req.size,
                message=str(exc),
            )
        except Exception as exc:
            return OrderResult(
                venue=self.id,
                client_order_id=req.client_order_id,
                status=ExecutionStatus.FAILED,
                requested_size=req.size,
                message=f"preflight failed: {type(exc).__name__}: {str(exc)[:180]}",
            )
        try:
            response = await self._signed_json("POST", "/orders", body)
        except httpx.TimeoutException:
            return self._unknown(req, "timeout")
        except httpx.NetworkError as exc:
            return self._unknown(req, f"network {type(exc).__name__}")
        except VenueHTTPError as exc:
            if exc.status_code == 409:
                return self._unknown(req, "duplicate clientOrderId; reconcile only")
            if 400 <= exc.status_code < 500:
                return OrderResult(
                    venue=self.id,
                    client_order_id=req.client_order_id,
                    status=ExecutionStatus.FAILED,
                    requested_size=req.size,
                    message=f"venue rejected ({exc.status_code}): {exc.body[:220]}",
                )
            return self._unknown(req, f"server error {exc.status_code}")
        except Exception as exc:
            return self._unknown(req, f"unexpected {type(exc).__name__}")
        order_data = response.get("order") or {}
        execution = response.get("execution") or {}
        settlement = str(execution.get("settlementStatus") or "UNKNOWN").upper()
        totals = execution.get("totalsRaw") or {}
        filled = to_decimal(totals.get("contractsGross") or 0) / Decimal("1000000")
        if settlement in {"MATCHED", "MINED", "CONFIRMED"}:
            status = ExecutionStatus.FILLED if filled >= req.size else ExecutionStatus.PARTIAL
        elif settlement == "DELAYED":
            status = ExecutionStatus.UNKNOWN
        elif settlement in {"UNMATCHED", "CANCELED"}:
            status = ExecutionStatus.NOT_FILLED
        elif settlement == "FAILED":
            status = ExecutionStatus.FAILED
        else:
            status = ExecutionStatus.UNKNOWN
        fee = to_decimal(totals.get("usdFee") or 0) / Decimal("1000000")
        return OrderResult(
            venue=self.id,
            client_order_id=req.client_order_id,
            venue_order_id=str(order_data.get("id") or "") or None,
            status=status,
            requested_size=req.size,
            filled_size=min(filled, req.size),
            remaining_size=max(ZERO, req.size - filled),
            average_price=price if filled > 0 else None,
            fee_usd=fee,
            message=f"settlement={settlement}" + ("; delayed, do not resend" if settlement == "DELAYED" else ""),
            raw_redacted={"settlementStatus": settlement, "order_id": order_data.get("id")},
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
            await self._signed_json("DELETE", f"/orders/{order_id}")
            return CancelResult(venue=self.id, ok=True, cancelled=1)
        except Exception as exc:
            return CancelResult(venue=self.id, ok=False, message=f"{type(exc).__name__}: {str(exc)[:160]}")

    async def cancel_all(self, ref: MarketRef | None = None) -> CancelResult:
        try:
            if ref is None:
                await self._signed_json("DELETE", "/orders/all")
                return CancelResult(venue=self.id, ok=True, cancelled=-1, message="cancel all acknowledged")
            orders = await self.get_open_orders(ref)
            cancelled = 0
            for order in orders:
                result = await self.cancel_order(order.order_id)
                if result.ok:
                    cancelled += 1
            return CancelResult(venue=self.id, ok=True, cancelled=cancelled)
        except Exception as exc:
            return CancelResult(venue=self.id, ok=False, message=f"{type(exc).__name__}: {str(exc)[:160]}")

    # ------------------------------------------------------------------- fee
    def fee(self, price: Decimal, size: Decimal, side: Side, market: Market) -> FeeQuote:
        del side
        rate = Decimal(str(market.fee_info.get("taker_rate", "0.02")))
        fee = (price * size * rate).quantize(Decimal("0.000001"))
        return FeeQuote(fee_usd=fee, rate=rate, source="Limitless conservative notional fee")

    # ---------------------------------------------------------------- doctor
    async def doctor(self) -> DoctorReport:
        checks: list[DoctorCheck] = []
        start = time.perf_counter()
        try:
            page = await self.list_markets(MarketQuery(limit=1))
            if not page.items:
                raise VenueUnavailableError("no active markets")
            market = page.items[0]
            book = await self.get_book(
                MarketRef(venue=self.id, market_id=market.native_id, outcome=Outcome.YES), depth=5
            )
            if not book.asks and not book.bids:
                raise VenueUnavailableError("orderbook empty")
            checks.append(
                DoctorCheck(
                    name="public_read",
                    status=DoctorStatus.OK,
                    detail=market.title[:60],
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
                    detail="HMAC token or wallet key absent; PAPER works",
                )
            )
        else:
            try:
                profile = await self._profile_data()
                balance = await self.get_balance()
                auth_ok = True
                checks.append(
                    DoctorCheck(
                        name="authenticated_read",
                        status=DoctorStatus.OK,
                        detail=f"profile {profile.get('id')} balance {balance.available_usd:.2f} USDC",
                    )
                )
                if profile.get("tradeWalletOption") == "smartWallet":
                    checks.append(
                        DoctorCheck(
                            name="wallet_mode",
                            status=DoctorStatus.UNAVAILABLE,
                            detail="profile uses smartWallet; self-signed API orders require eoa",
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
                    detail="opt-in only; Limitless has no demo venue",
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
            reason="" if auth_ok else "credentials absent or auth failed; PAPER only",
        )

    async def _doctor_order(self) -> DoctorCheck:
        try:
            market = (await self.list_markets(MarketQuery(limit=1))).items[0]
            request = OrderRequest(
                venue=self.id,
                market_id=market.native_id,
                outcome=Outcome.YES,
                side=Side.BUY,
                price=Decimal("0.001"),
                size=Decimal("1"),
                client_order_id="doctor-" + uuid.uuid4().hex,
                order_type="GTC",
            )
            result = await self.place_order(request)
            if result.venue_order_id:
                await self.cancel_order(result.venue_order_id)
            ok = result.status not in {ExecutionStatus.FAILED, ExecutionStatus.ERROR}
            return DoctorCheck(
                name="order_roundtrip",
                status=DoctorStatus.OK if ok else DoctorStatus.DEGRADED,
                detail=f"place={result.status.value} cancel=attempted",
            )
        except Exception as exc:
            return DoctorCheck(name="order_roundtrip", status=DoctorStatus.DEGRADED, detail=str(exc)[:200])
