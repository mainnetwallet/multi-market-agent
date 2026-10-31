"""Polymarket adapter: Gamma discovery, CLOB books, official SDK for account/orders."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from .. import discover as discover_lib
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
    OrderType,
    Outcome,
    OutcomeInfo,
    Position,
    Side,
    VenueCaps,
    VenueId,
    VenueOrder,
    VenueTrade,
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

EVENT_TAG_TTL_SECONDS = 6 * 3600
EVENT_TAG_RETRY_SECONDS = 60

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


def _labels(items: Any) -> list[str]:
    """Labels and slugs from a Gamma list of ``{"label": ..., "slug": ...}`` objects (or plain strings)."""
    out: list[str] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if isinstance(item, dict):
            out.extend(str(item[key]) for key in ("label", "slug") if item.get(key))
        elif item:
            out.append(str(item))
    return out


def resolve_category(raw: dict[str, Any], event: dict[str, Any]) -> str:
    """Pick the fee category for a Gamma market.

    Gamma fills ``category`` on some markets and leaves it empty on others, so look at
    every place it can appear: the market's ``category``, its ``categories`` and ``tags``,
    then the parent event's ``category`` and ``tags``. A name that is in the fee schedule
    wins over one that is not. If several schedule names appear, the one with the highest
    taker rate is used, so a fee is never underestimated. With nothing usable the result is
    the first non-empty name found, or "Other".
    """
    named: list[str] = []
    for source in (raw.get("category"), event.get("category")):
        if source and str(source).strip():
            named.append(str(source).strip())
    tagged = _labels(raw.get("categories")) + _labels(raw.get("tags"))
    tagged += _labels(event.get("categories")) + _labels(event.get("tags"))
    known = {
        key
        for key in (name.strip().lower() for name in (*named, *tagged))
        if key in CATEGORY_FEE_RATES and key != "other"
    }
    if known:
        return max(known, key=lambda key: CATEGORY_FEE_RATES[key]).title()
    return named[0] if named else "Other"


def _fee_info(
    category: str,
    schedule: dict[str, Any] | None,
    fees_enabled: Any,
    *,
    lookup_failed: bool = False,
) -> dict[str, Any]:
    """Fee fields for a market.

    The taker rate is the category's rate from the fee schedule, raised to the market's own
    ``feeSchedule.rate`` when that is higher. If the category could not be read at all
    (``lookup_failed``) the highest documented rate is assumed, so a fee is never understated.
    """
    rate = CATEGORY_FEE_RATES.get(category.lower(), CATEGORY_FEE_RATES["other"])
    if lookup_failed:
        rate = max(CATEGORY_FEE_RATES.values())
    schedule_rate = (schedule or {}).get("rate")
    if schedule_rate is not None:
        try:
            rate = max(rate, Decimal(str(schedule_rate)))
        except Exception:
            rate = max(rate, max(CATEGORY_FEE_RATES.values()))
    info: dict[str, Any] = {
        "taker_rate": str(rate),
        "maker_rate": "0",
        "category": category,
        "source": "polymarket category fee schedule 2026-10-30",
        "fees_enabled": fees_enabled,
        "fee_schedule": schedule,
    }
    if lookup_failed:
        info["category_lookup"] = "failed"
    return info


def _handle_summary(handle: Any) -> dict[str, Any]:
    """Reduce a SDK transaction handle to the few fields the operator needs."""

    if handle is None:
        return {}
    out: dict[str, Any] = {"type": type(handle).__name__}
    for field in ("transaction_hash", "transaction_id", "state", "status"):
        value = getattr(handle, field, None)
        if value is not None:
            out[field] = str(value)
    return out


class PolymarketVenue(HttpVenue):
    id = VenueId.POLYMARKET
    # Capabilities verified against docs.polymarket.com on 2026-10-30.
    caps = VenueCaps(
        realtime=True,
        sandbox=False,
        neg_risk=True,
        min_order_shares=Decimal("5"),
        min_notional_usd=Decimal("1"),
        default_tick=Decimal("0.01"),
        order_types=(OrderType.GTC, OrderType.GTD, OrderType.FAK, OrderType.FOK),
        # The docs: an expiry must be at least 3 minutes away, and the order
        # expires one minute before it.
        min_expiry_minutes=3,
        post_only=True,
        amend=False,
        batch_orders=True,
        approvals_needed=True,
        collateral_asset="pUSD",
        deposit=True,
        withdraw=True,
        redeem=True,
        funds_history=True,
        unsupported_reason={
            "amend": "Polymarket has no amend endpoint; cancel and place a new order instead.",
        },
    )

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._markets: dict[str, Market] = {}
        self._asset_index: dict[str, tuple[str, Outcome]] = {}
        self._condition_index: dict[str, str] = {}
        self._event_tag_cache: dict[str, tuple[float, list[Any] | None]] = {}
        self._tag_gate = asyncio.Semaphore(4)
        # website category slug -> numeric Gamma tag id (ids never change, so kept for the process lifetime)
        self._nav_tag_ids: dict[str, str] = {}
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
        parsed: list[Market] = []
        for raw in data:
            try:
                parsed.append(self.parse_gamma_market(raw))
            except Exception:
                continue
        markets: list[Market] = []
        for market in await self.with_event_categories(parsed):
            if query.q and query.q.lower() not in market.title.lower():
                continue
            self._remember(market)
            markets.append(market)
        next_cursor = str(offset + len(data)) if len(data) >= limit else None
        return Page(items=markets[: query.limit], next_cursor=next_cursor)

    async def _nav_tag_id(self, slug: str) -> str:
        """Numeric Gamma tag id for a category slug (``GET /tags/slug/{slug}``), as the docs describe for filtering."""

        cached = self._nav_tag_ids.get(slug)
        if cached:
            return cached
        data = await self.get_json(GAMMA + "/tags/slug/" + slug)
        tag_id = str(data.get("id") or "") if isinstance(data, dict) else ""
        if not tag_id:
            raise VenueUnavailableError(f"Polymarket has no tag with slug {slug!r}")
        self._nav_tag_ids[slug] = tag_id
        return tag_id

    async def discover_tabs(self) -> dict[str, Any]:
        """Tabs for the app: the sort modes, then the categories polymarket.com shows in its header."""

        return {"tabs": discover_lib.polymarket_tab_list(), "note": ""}

    async def discover(self, *, tab: str, q: str | None, limit: int, offset: int) -> dict[str, Any]:
        """Polymarket-style browse: Gamma events for a tab (or a text search) as cards."""

        limit = min(max(limit, 1), 50)
        offset = max(offset, 0)
        cfg = discover_lib.polymarket_tab_cfg(tab)
        if cfg is None:
            raise ValueError(f"unknown tab: {tab}")
        query = (q or "").strip()
        if query:
            page = offset // limit + 1
            data = await self.get_json(
                GAMMA + "/public-search",
                params={"q": query, "limit_per_type": limit, "page": page, "events_status": "active"},
            )
            rows = data.get("events") if isinstance(data, dict) else None
            more = bool(isinstance(data, dict) and (data.get("pagination") or {}).get("hasMore"))
        else:
            fetch = int(cfg.get("fetch", limit))
            params: dict[str, Any] = {
                "active": "true",
                "closed": "false",
                "order": cfg.get("order", "volume24hr"),
                "ascending": "false",
                "limit": fetch,
                "offset": offset,
            }
            if cfg.get("tag_slug"):
                try:
                    params["tag_id"] = await self._nav_tag_id(str(cfg["tag_slug"]))
                except Exception as exc:
                    return {
                        "tab": tab,
                        "items": [],
                        "next_offset": None,
                        "note": f"Could not load the {cfg['label']} category: {str(exc)[:100]}",
                    }
            rows = await self.get_json(GAMMA + "/events", params=params)
            more = isinstance(rows, list) and len(rows) >= fetch
        if not isinstance(rows, list):
            raise VenueUnavailableError("Polymarket Gamma returned a non-list events payload")
        items = discover_lib.shape_events(rows)
        if cfg.get("rank") == "change" and not query:
            items = discover_lib.rank_by_change(items)
        items = items[:limit]
        # Warm the adapter so tapping a card opens the trade sheet without another lookup.
        by_id = {
            str(m.get("id")): m
            for event in rows
            if isinstance(event, dict)
            for m in event.get("markets") or []
            if isinstance(m, dict)
        }
        for item in items:
            for card in item["markets"]:
                raw = by_id.get(card["market_id"])
                if raw is None:
                    continue
                try:
                    self._remember(self.parse_gamma_market(raw))
                except Exception:
                    continue
        step = int(cfg.get("fetch", limit)) if not query else limit
        note = (
            "No open markets under this category right now." if not items and not query and cfg.get("tag_slug") else ""
        )
        return {"tab": tab, "items": items, "next_offset": offset + step if more else None, "note": note}

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
        events = raw.get("events") or []
        event = events[0] if events and isinstance(events[0], dict) else {}
        category = resolve_category(raw, event)
        tick_raw = raw.get("orderPriceMinTickSize") or raw.get("minimumTickSize") or "0.01"
        tick = Decimal(str(tick_raw))
        schedule = raw.get("feeSchedule") if isinstance(raw.get("feeSchedule"), dict) else None
        fee_info = _fee_info(category, schedule, raw.get("feesEnabled"))
        # Gamma often sends no category at all; get_market/list_markets then read the event's tags.
        fee_info["category_resolved"] = category != "Other"
        outcomes = (
            OutcomeInfo(outcome=Outcome.YES, native_id=asset_ids[0], label=labels[0]),
            OutcomeInfo(outcome=Outcome.NO, native_id=asset_ids[1], label=labels[1]),
        )
        condition_id = str(raw.get("conditionId") or "")
        if condition_id:
            self._condition_index[market_id] = condition_id
        resolution = None
        if closed:
            prices = self._json_list(raw.get("outcomePrices"))
            try:
                if len(prices) >= 2 and Decimal(str(prices[0])) >= Decimal("0.999"):
                    resolution = Outcome.YES
                elif len(prices) >= 2 and Decimal(str(prices[1])) >= Decimal("0.999"):
                    resolution = Outcome.NO
            except Exception:
                resolution = None
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
            resolution_outcome=resolution,
        )

    def _remember(self, market: Market) -> None:
        self._markets[market.native_id] = market
        for info in market.outcomes:
            self._asset_index[info.native_id] = (market.native_id, info.outcome)

    async def _event_tags(self, event_id: str) -> list[Any] | None:
        """Tags of an event from ``GET /events/{id}/tags``; None when the lookup failed.

        Tags rarely change, so a good answer is kept for hours; a failure is only
        remembered briefly so the next scan tries again without hammering Gamma.
        """
        cached = self._event_tag_cache.get(event_id)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            async with self._tag_gate:
                data = await self.get_json(GAMMA + f"/events/{event_id}/tags")
            tags = data if isinstance(data, list) else None
        except Exception:
            tags = None
        ttl = EVENT_TAG_TTL_SECONDS if tags is not None else EVENT_TAG_RETRY_SECONDS
        self._event_tag_cache[event_id] = (now + ttl, tags)
        return tags

    async def with_event_categories(self, markets: Sequence[Market]) -> list[Market]:
        """Fill in the fee category of markets whose Gamma row carried none, from their event's tags."""
        pending = {m.event_id for m in markets if m.event_id and not m.fee_info.get("category_resolved")}
        if not pending:
            return list(markets)
        await asyncio.gather(*(self._event_tags(str(event_id)) for event_id in pending))
        out: list[Market] = []
        for market in markets:
            if not market.event_id or market.fee_info.get("category_resolved"):
                out.append(market)
                continue
            tags = await self._event_tags(market.event_id)
            schedule = market.fee_info.get("fee_schedule")
            enabled = market.fee_info.get("fees_enabled")
            if tags is None:
                info = _fee_info(market.category, schedule, enabled, lookup_failed=True)
                info["category_resolved"] = False
                out.append(market.model_copy(update={"fee_info": info}))
                continue
            category = resolve_category({}, {"tags": tags})
            info = _fee_info(category, schedule, enabled)
            info["category_resolved"] = True
            out.append(market.model_copy(update={"category": category, "fee_info": info}))
        return out

    async def get_market(self, ref: MarketRef) -> Market:
        market = self._markets.get(ref.market_id)
        if market is None:
            data = await self.get_json(GAMMA + f"/markets/{ref.market_id}")
            market = self.parse_gamma_market(data)
        # A market remembered from a browse card may still lack its category; fill it in.
        if market.event_id and not market.fee_info.get("category_resolved"):
            market = (await self.with_event_categories([market]))[0]
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

    async def get_recent_trades(self, ref: MarketRef) -> list[VenueTrade]:
        client = await self._secure()
        market = await self.get_market(ref)
        asset_id = market.outcome(ref.outcome or Outcome.YES).native_id
        page = await client.list_account_trades(asset_id=asset_id).first_page()
        mapping = self._asset_index.get(asset_id)
        out: list[VenueTrade] = []
        for trade in page.items:
            if str(trade.asset_id) != asset_id:
                continue
            out.append(
                VenueTrade(
                    venue=self.id,
                    market_id=mapping[0] if mapping else ref.market_id,
                    trade_id=str(trade.id),
                    order_id=str(trade.taker_order_id) if trade.taker_order_id else None,
                    outcome=mapping[1] if mapping else ref.outcome,
                    side=Side(trade.side),
                    price=trade.price,
                    size=trade.size,
                    created_at=trade.matched_at,
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
            if req.order_type in (OrderType.FOK, OrderType.FAK):
                # A market order is a limit order whose price bound rides in the signed
                # amounts; the SDK takes that bound as max_price / min_price.
                accepted = await client.place_market_order(
                    asset_id=asset_id,
                    side=req.side.value,
                    shares=size,
                    order_type="FOK" if req.order_type is OrderType.FOK else "FAK",
                    **({"max_price": price} if req.side is Side.BUY else {"min_price": price}),
                )
            else:
                expiration = int(req.expires_at.timestamp()) if req.expires_at else None
                accepted = await client.place_limit_order(
                    asset_id=asset_id,
                    price=price,
                    size=size,
                    side=req.side.value,
                    post_only=req.post_only,
                    expiration=expiration,
                )
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

    async def redeem_positions(self, *, market_id: str, outcome: Any = None) -> dict[str, Any]:
        """Redeem resolved positions through the official SDK (Relayer or EOA path)."""

        client = await self._secure()
        market = await self.get_market(MarketRef(venue=self.id, market_id=market_id))
        condition_id = self._condition_index.get(market_id)
        handle = await client.redeem_positions(
            condition_id=condition_id,
            market_id=market_id,
            position_id=market.outcome(outcome or Outcome.YES).native_id if outcome else None,
        )
        return {"supported": True, "handle": _handle_summary(handle)}

    async def split_merge(self, *, market_id: str, action: str) -> dict[str, Any]:
        """Split a full outcome set or merge it back into collateral."""

        client = await self._secure()
        market = await self.get_market(MarketRef(venue=self.id, market_id=market_id))
        condition_id = self._condition_index.get(market_id)
        legs = [info.native_id for info in market.outcomes]
        if action == "split":
            handle = await client.split_position(condition_id=condition_id, legs=legs, amount=0)
        else:
            handle = await client.merge_positions(condition_id=condition_id, legs=legs, amount="max")
        return {"supported": True, "action": action, "handle": _handle_summary(handle)}

    async def get_price_history(self, market_id: str, *, interval: str = "1d") -> list[dict[str, Any]]:
        """CLOB /prices-history for the market's YES asset."""

        market = await self.get_market(MarketRef(venue=self.id, market_id=market_id))
        asset_id = market.outcome(Outcome.YES).native_id
        data = await self.get_json(CLOB + "/prices-history", params={"token_id": asset_id, "interval": interval})
        out: list[dict[str, Any]] = []
        for row in data.get("history") or []:
            try:
                out.append(
                    {
                        "at": datetime.fromtimestamp(int(row["t"]), tz=UTC).isoformat(),
                        "open": str(to_decimal(row.get("p") or 0)),
                        "high": str(to_decimal(row.get("p") or 0)),
                        "low": str(to_decimal(row.get("p") or 0)),
                        "close": str(to_decimal(row.get("p") or 0)),
                        "volume": str(to_decimal(row.get("v") or 0)),
                    }
                )
            except Exception:
                continue
        return out

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
                    detail="no signer key; public reads only",
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
            reason="" if auth_ok else "signer absent or auth failed; trading disabled",
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
