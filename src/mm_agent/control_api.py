"""FastAPI Control API, session auth, WebSocket events and SSE chat."""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import os
import time
from collections import defaultdict, deque
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field

from . import discover as discover_lib
from . import eventview, eventview_source
from .agent import AgentService
from .chat import ChatService
from .config import Settings
from .domain import OrderType, Outcome, Side, VenueId, utcnow
from .events import EventBus
from .full_control import FullControlError, FullControlService
from .funds import FundsError, FundsService
from .live.hub import LiveHub
from .live.service import build_hub
from .polymarket_data import DataAPIError
from .scanner import Scanner
from .store import ConversationBusy, ConversationNotFound
from .util import get_logger, redact
from .venues import polymarket_signing
from .venues.registry import VenueRegistry

logger = get_logger(__name__)
STATIC = Path(__file__).parent / "static"
SESSION_COOKIE = "mm_session"
# A session renews while the app is used, but never lives longer than this in total.
SESSION_MAX_AGE_SECONDS = 90 * 24 * 3600


class LoginBody(BaseModel):
    username: str
    password: str


class SwitchBody(BaseModel):
    venue: VenueId


class AnalyzeBody(BaseModel):
    venue: VenueId | None = None
    market_id: str
    outcome: Outcome | None = None
    amount_usd: Decimal | None = None
    execute: bool = False


class ApproveBody(BaseModel):
    amount_usd: Decimal | None = None


class CloseBody(BaseModel):
    market_id: str
    outcome: Outcome | None = None
    amount_usd: Decimal | None = None


class RuleBody(BaseModel):
    market_id: str
    outcome: Outcome
    kind: str
    trigger_price: Decimal | None = None
    trigger_at: datetime | None = None


_CHAT_TASKS: set[asyncio.Task[Any]] = set()
CHAT_PAGE_SIZE = 50


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=36)


class ConversationBody(BaseModel):
    title: str = Field(default="", max_length=120)


class LimitBody(BaseModel):
    name: str
    value: str


class PanicBody(BaseModel):
    confirm: bool = False


class StepupBody(BaseModel):
    password: str
    action: str = "withdraw"


class CeilingBody(BaseModel):
    name: str
    value: Decimal


class LimitOrderBody(BaseModel):
    venue: VenueId | None = None
    market_id: str
    outcome: Outcome
    side: Side = Side.BUY
    price: Decimal | None = None
    shares: Decimal | None = None
    amount_usd: Decimal | None = None
    tif: OrderType = OrderType.GTC
    expires_at: datetime | None = None
    post_only: bool = False
    # The venue's client order id; it lands in an 80-character column.
    client_id: str | None = Field(default=None, max_length=64)


class MarketOrderBody(BaseModel):
    venue: VenueId | None = None
    market_id: str
    outcome: Outcome
    side: Side = Side.BUY
    shares: Decimal | None = None
    amount_usd: Decimal | None = None
    max_slippage_cents: Decimal = Decimal("2")
    client_id: str | None = Field(default=None, max_length=64)


class SellBody(BaseModel):
    venue: VenueId | None = None
    market_id: str
    outcome: Outcome
    shares: Decimal
    price: Decimal | None = None
    client_id: str | None = None


class AmendBody(BaseModel):
    price: Decimal | None = None
    shares: Decimal | None = None


class CancelAllBody(BaseModel):
    venue: VenueId | None = None
    market_id: str | None = None


class RedeemBody(BaseModel):
    venue: VenueId | None = None
    market_id: str
    outcome: Outcome | None = None


class AllowlistBody(BaseModel):
    label: str
    address: str
    network: str
    asset: str


class WithdrawPreviewBody(BaseModel):
    venue: VenueId
    amount: Decimal
    address_id: str | None = None
    recipient: str | None = None
    network: str | None = None
    asset: str | None = None


class WithdrawBody(BaseModel):
    venue: VenueId
    amount: Decimal
    address_id: str | None = None
    recipient: str | None = None
    network: str | None = None
    asset: str | None = None


class ApprovalBody(BaseModel):
    venue: VenueId
    kind: str


def _data_http_error(exc: DataAPIError) -> HTTPException:
    """A venue error as an HTTP error: the venue's own 400/404/429/503 are kept, anything else is a 502."""

    status = exc.status if exc.status in (400, 404, 429, 503) else 502
    headers = {"Retry-After": str(math.ceil(exc.retry_after))} if exc.retry_after else None
    return HTTPException(status_code=status, detail=exc.message[:200] or "venue error", headers=headers)


def _book_json(book: Any) -> dict[str, Any]:
    """A book snapshot for the UI. `best_bid`/`best_ask`/`mid`/`spread` are plain properties on the model,
    so `model_dump` leaves them out; the UI's price boxes need them, so they are added here."""

    data: dict[str, Any] = book.model_dump(mode="json")
    for name in ("best_bid", "best_ask", "mid", "spread"):
        value = getattr(book, name)
        data[name] = None if value is None else str(value)
    data["age_seconds"] = round(book.age_seconds(), 2)
    return data


def create_app(
    settings: Settings,
    agent: AgentService,
    scanner: Scanner,
    registry: VenueRegistry,
    chat: ChatService,
    bus: EventBus,
    *,
    started_at: datetime | None = None,
    lifespan: Any = None,
    hub: LiveHub | None = None,
) -> FastAPI:
    serializer = URLSafeTimedSerializer(settings.session_secret or "unset-secret", salt="mm-session")
    own_hub = hub is None  # a hub the caller did not start is started on the first stream connection
    live_hub = hub if hub is not None else build_hub(registry, run_feeds=False)
    login_attempts: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=10))
    full = FullControlService(settings, agent.store, registry, agent, agent.execution)
    full.restore_ceilings()
    agent.full_control = full
    funds = FundsService(settings, agent.store)
    agent.funds = funds
    start = started_at or utcnow()
    del start
    app = FastAPI(
        title="Multi-Market Prediction Agent",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/v1") else "no-cache"
        if request.url.path.startswith("/v1") or request.url.path in {"/", "/index.html"}:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self' ws: wss:; font-src 'self'; frame-ancestors 'none'"
            )
        return response

    def _username_from_token(token: str) -> str | None:
        try:
            data = serializer.loads(token, max_age=settings.session_ttl_hours * 3600)
        except (SignatureExpired, BadSignature):
            return None
        if not isinstance(data, dict):
            return None
        if not hmac.compare_digest(str(data.get("v", "")), "1"):
            return None
        return str(data.get("u") or "") or None

    async def require_auth(request: Request) -> str:
        token = request.cookies.get(SESSION_COOKIE)
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
        if not token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
        assert token is not None
        username = _username_from_token(token)
        if username is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or expired session")
        return username

    def actor(request: Request) -> str:
        token = request.cookies.get(SESSION_COOKIE)
        username = _username_from_token(token) if token else "api"
        return f"web:{username or 'api'}"

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, Any]:
        # Render sets RENDER_GIT_COMMIT for the running deploy; CI waits for it before publishing the APK.
        return {
            "status": "ok",
            "time": utcnow().isoformat(),
            "version": "0.1.0",
            "commit": os.environ.get("RENDER_GIT_COMMIT", ""),
        }

    @app.head("/health", include_in_schema=False)
    async def health_head() -> Response:
        return Response(status_code=200)

    @app.get("/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        db = agent.store.healthcheck()
        active = registry.active_or_none
        ready_bool = bool(db and active and registry.is_ready(active))
        payload = {
            "ready": ready_bool,
            "db": db,
            "active_venue": active.value if active else None,
            "active_ready": bool(active and registry.is_ready(active)),
            "unit_guard": agent.unit_guard.ok,
            "feed_errors": {k: v.error for k, v in scanner.feed_diag().venues.items() if v.error},
        }
        return JSONResponse(payload, status_code=200 if ready_bool else 503)

    def _issue_session(response: Response, user: str, origin: int) -> str:
        """Sign a session token and set the cookie. `o` is when the operator really signed in."""
        token = serializer.dumps({"u": user, "v": "1", "o": origin})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=settings.session_ttl_hours * 3600,
            httponly=True,
            samesite="strict",
            secure=settings.app_env.lower() in {"prod", "production"},
            path="/",
        )
        return token

    @app.post("/v1/auth/login")
    async def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
        if not settings.admin_password or not settings.session_secret:
            raise HTTPException(status_code=503, detail="ADMIN_PASSWORD and SESSION_SECRET must be configured")
        client = request.client.host if request.client else "unknown"
        now = time.time()
        bucket = login_attempts[client]
        while bucket and bucket[0] < now - 300:
            bucket.popleft()
        if len(bucket) >= 8:
            raise HTTPException(status_code=429, detail="too many login attempts; wait 5 minutes")
        bucket.append(now)
        user_ok = hmac.compare_digest(body.username, settings.admin_username)
        pass_ok = hmac.compare_digest(body.password, settings.admin_password)
        if not (user_ok and pass_ok):
            logger.warning("login_failed", client=client)
            raise HTTPException(status_code=401, detail="invalid credentials")
        token = _issue_session(response, settings.admin_username, int(time.time()))
        agent.store.audit(f"web:{settings.admin_username}", "auth.login", {})
        return {"ok": True, "username": settings.admin_username, "token": token}

    @app.post("/v1/auth/refresh")
    async def refresh_session(
        request: Request, response: Response, user: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Swap a still-valid session for a fresh one, so a phone that keeps using the app stays signed in.

        The original sign-in time travels with the token: after SESSION_MAX_AGE_SECONDS the operator
        signs in again, however often the app was used.
        """
        token = request.cookies.get(SESSION_COOKIE)
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
        try:
            data, issued_at = serializer.loads(
                token or "", max_age=settings.session_ttl_hours * 3600, return_timestamp=True
            )
        except (SignatureExpired, BadSignature):
            raise HTTPException(status_code=401, detail="invalid or expired session") from None
        origin = int(data.get("o") or issued_at.timestamp()) if isinstance(data, dict) else 0
        if time.time() - origin > SESSION_MAX_AGE_SECONDS:
            raise HTTPException(status_code=401, detail="invalid or expired session")
        return {"ok": True, "username": user, "token": _issue_session(response, user, origin)}

    @app.post("/v1/auth/logout")
    async def logout(response: Response, _: str = Depends(require_auth)) -> dict[str, bool]:
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/v1/auth/me")
    async def me(user: str = Depends(require_auth)) -> dict[str, Any]:
        return {
            "username": user,
            "badge": _badge(),
            "active_venue": registry.active_or_none.value if registry.active_or_none else None,
        }

    def _status_payload() -> dict[str, Any]:
        payload = agent.status()
        if settings.live_views_enabled:
            payload["live"] = live_hub.metrics()
        return payload

    @app.get("/v1/status")
    async def get_status(_: str = Depends(require_auth)) -> dict[str, Any]:
        return _status_payload()

    @app.get("/status")
    async def get_status_short(_: str = Depends(require_auth)) -> dict[str, Any]:
        return _status_payload()

    @app.get("/v1/venues")
    async def venues(_: str = Depends(require_auth)) -> dict[str, Any]:
        return registry.summary()

    @app.post("/v1/venues/active")
    async def switch_venue(body: SwitchBody, request: Request, _: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return await registry.switch(body.venue, actor=actor(request))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/markets")
    async def markets(
        q: str | None = None,
        venue: VenueId | None = None,
        limit: int = 50,
        _: str = Depends(require_auth),
    ) -> dict[str, Any]:
        rows = agent.store.list_markets(venue=venue, q=q, limit=min(max(limit, 1), 200))
        return {"markets": [m.model_dump(mode="json") for m in rows], "count": len(rows)}

    def _discover_venue(venue: VenueId | None) -> VenueId:
        chosen = venue or registry.active_or_none or VenueId.POLYMARKET
        if venue is None and chosen.value not in discover_lib.VENUE_TABS:
            chosen = VenueId.POLYMARKET  # e.g. the fake test venue is active: keep the old default
        if chosen.value not in discover_lib.VENUE_TABS:
            raise HTTPException(status_code=404, detail=f"no browse feed for {chosen.value}")
        return chosen

    @app.get("/v1/discover/tabs")
    async def discover_tabs(venue: VenueId | None = None, _: str = Depends(require_auth)) -> dict[str, Any]:
        chosen = _discover_venue(venue)
        adapter = registry.adapters.get(chosen)
        if chosen in registry.enabled() and adapter is not None and hasattr(adapter, "discover_tabs"):
            # Kalshi/Limitless categories come from the venue's API; Polymarket's follow its website bar.
            result: dict[str, Any] = await adapter.discover_tabs()
            return {"venue": chosen.value, **result}
        if chosen == VenueId.POLYMARKET:
            return {"venue": chosen.value, "tabs": discover_lib.polymarket_tab_list()}
        return {"venue": chosen.value, "tabs": discover_lib.tab_list(chosen.value)}

    @app.get("/v1/discover")
    async def discover(
        tab: str = "trending",
        q: str | None = None,
        limit: int = 30,
        offset: int = 0,
        venue: VenueId | None = None,
        _: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Polymarket-style browse (tabs, prices, volume). Read-only public data, per venue."""

        chosen = _discover_venue(venue)
        adapter = registry.adapters.get(chosen)
        if chosen not in registry.enabled() or adapter is None or not hasattr(adapter, "discover"):
            raise HTTPException(status_code=409, detail=f"{chosen.value} is not enabled on this server")
        try:
            result: dict[str, Any] = await adapter.discover(tab=tab, q=q, limit=limit, offset=offset)
            result["venue"] = chosen.value
            return result
        except ValueError as exc:  # an unknown tab id (category tabs are not known statically)
            raise HTTPException(status_code=404, detail=str(exc)[:160]) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"{chosen.value} discovery failed: {str(exc)[:160]}") from exc

    @app.get("/v1/market")
    async def market_detail(
        market_id: str, venue: VenueId | None = None, _: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            market = await agent.get_market(market_id, venue)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)[:200]) from exc
        books = await agent.books_for(market, force=True)
        research = agent.store.latest_research(market.venue, market.native_id)
        return {
            "market": market.model_dump(mode="json"),
            "books": {k.value: _book_json(v) for k, v in books.items()},
            "research": research,
            "event": await _live_event(market.venue, market.event_id or "", ""),
            "badge": _badge(),
        }

    async def _live_event(venue: VenueId, event_id: str, slug: str) -> dict[str, Any] | None:
        """The Polymarket event behind a market as a card (sports score, crypto window). None when it
        has none or Gamma is slow: the sheet then draws the plain market view."""

        adapter = registry.adapters.get(venue)
        if venue != VenueId.POLYMARKET or adapter is None or not hasattr(adapter, "event_live"):
            return None
        if not event_id and not slug:
            return None
        try:
            item: dict[str, Any] | None = await asyncio.wait_for(adapter.event_live(event_id, slug=slug), 4.0)
            return item
        except Exception:
            return None

    @app.get("/v1/live/event")
    async def live_event(
        event_id: str = "", slug: str = "", venue: VenueId = VenueId.POLYMARKET, _: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One event fresh from the venue: score/period/prices for a game, window and prices for a crypto
        Up or Down market. The app polls this while a game or window is on screen."""

        if not event_id and not slug:
            raise HTTPException(status_code=400, detail="event_id or slug is required")
        adapter = registry.adapters.get(venue)
        if venue != VenueId.POLYMARKET or adapter is None or not hasattr(adapter, "event_live"):
            raise HTTPException(status_code=404, detail=f"no live event feed for {venue.value}")
        try:
            item = await adapter.event_live(event_id, slug=slug)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)[:120]) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"event lookup failed: {str(exc)[:160]}") from exc
        if item is None:
            raise HTTPException(status_code=404, detail="event not found")
        return {"ok": True, "item": item}

    @app.get("/v1/event/view")
    async def event_view(
        venue: VenueId | None = None,
        event_id: str = Query("", alias="id", max_length=130),
        slug: str = Query("", max_length=130),
        url: str = Query("", max_length=300),
        _: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """One event as an EventView: layout, rows, sections, chart shell. Any open market renders; an
        unknown shape comes back as ``generic_list``. Display data only: orders use ``/v1/orders/*``."""

        if not settings.live_views_enabled:
            raise HTTPException(status_code=404, detail="live views are disabled")
        try:
            if url:
                parsed_venue, ident = eventview.parse_event_url(url)
                venue, slug, event_id = VenueId(parsed_venue), ident, ""
            venue = venue or registry.active_or_none or VenueId.POLYMARKET
            adapter = registry.adapters.get(venue)
            if adapter is None:
                raise HTTPException(status_code=404, detail=f"{venue.value} is not enabled")
            if not event_id and not slug:
                raise ValueError("id, slug or url is required")
            if venue == VenueId.POLYMARKET:
                view = await eventview_source.polymarket_view(adapter, event_id=event_id, slug=slug)
            elif venue == VenueId.KALSHI:
                view = await eventview_source.kalshi_view(adapter, ticker=(slug or event_id).upper())
            else:
                view = await eventview_source.limitless_view(adapter, slug=slug or event_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)[:160]) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"event lookup failed: {str(exc)[:160]}") from exc
        if view is None:
            raise HTTPException(status_code=404, detail="event not found")
        return {"ok": True, "view": view}

    # ── Polymarket public data (Data API v2 and CLOB price routes; no credentials) ──
    def _pm_adapter(attr: str) -> Any:
        adapter = registry.adapters.get(VenueId.POLYMARKET)
        client = getattr(adapter, attr, None)
        if client is None:
            raise HTTPException(status_code=404, detail="polymarket data is not available")
        return client

    async def _pm_call(call: Any) -> dict[str, Any]:
        try:
            return {"ok": True, "result": await call}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)[:200]) from exc
        except DataAPIError as exc:
            raise _data_http_error(exc) from exc
        except httpx.HTTPError as exc:  # timeout, DNS or connection failure: the venue was not reached
            raise HTTPException(status_code=502, detail=f"polymarket unreachable: {type(exc).__name__}") from exc
        except RuntimeError as exc:  # the adapter's HTTP client is not started yet
            raise HTTPException(status_code=503, detail=str(exc)[:120]) from exc

    @app.get("/v1/polymarket/data")
    async def polymarket_data_routes(_: str = Depends(require_auth)) -> dict[str, Any]:
        """Which Data API v2 routes exist and the query parameters each accepts."""

        from .polymarket_data import ENDPOINTS

        return {
            "ok": True,
            "routes": {name: sorted(ep.params) for name, ep in ENDPOINTS.items()},
            "prices": ["midpoint", "midpoints", "spread", "last-trade", "last-trades", "price"],
        }

    @app.get("/v1/polymarket/data/{name}")
    async def polymarket_data(name: str, request: Request, _: str = Depends(require_auth)) -> dict[str, Any]:
        """Read-only Data API v2: positions, trades, activity, holders, open interest, leaderboard,
        resolutions, token lookup, deep price history and more. Parameters are validated first;
        paging is by ``pagination.next_cursor`` (send it back as ``cursor``)."""

        client = _pm_adapter("market_data")
        return await _pm_call(client.fetch(name, dict(request.query_params)))

    @app.get("/v1/polymarket/price/{kind}")
    async def polymarket_price(
        kind: str, token_id: str = "", token_ids: str = "", side: str = "", _: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """CLOB public prices for outcome tokens: midpoint, spread, last trade, best price (needs
        ``side``), and the batch forms ``midpoints`` and ``last-trades`` (``token_ids``, comma-separated)."""

        clob = _pm_adapter("clob_public")
        calls: dict[str, Any] = {
            "midpoint": lambda: clob.midpoint(token_id),
            "spread": lambda: clob.spread(token_id),
            "last-trade": lambda: clob.last_trade_price(token_id),
            "price": lambda: clob.price(token_id, side),
            "midpoints": lambda: clob.midpoints(token_ids),
            "last-trades": lambda: clob.last_trades_prices(token_ids),
        }
        if kind not in calls:
            raise HTTPException(status_code=404, detail=f"unknown price route; use one of: {', '.join(calls)}")
        return await _pm_call(calls[kind]())

    @app.get("/v1/crypto/spot")
    async def crypto_spot(
        symbol: str, window_start: int | None = None, _: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Live coin price (public Coinbase data) with a short history, for the Up or Down windows."""

        adapter = registry.adapters.get(VenueId.POLYMARKET)
        feed = getattr(adapter, "spot", None)
        if feed is None:
            raise HTTPException(status_code=404, detail="the Polymarket venue is not enabled")
        try:
            snap: dict[str, Any] = await feed.snapshot(symbol, window_start=window_start)
            return snap
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)[:120]) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"price feed unavailable: {str(exc)[:120]}") from exc

    @app.get("/v1/book")
    async def book_snapshot(
        market_id: str, venue: VenueId | None = None, _: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Order books only (no research), for the UI's live ladder.

        Served from the cache when the snapshot is at most 1 s old, otherwise fetched from the venue, so
        several open tabs cannot multiply the venue load. Each book carries its own `timestamp`,
        `age_seconds` and `source`, so the UI can show how fresh the numbers really are.
        """

        try:
            market = await agent.get_market(market_id, venue)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)[:200]) from exc
        books = await agent.books_for(market, max_age=1)
        if not books:
            raise HTTPException(status_code=502, detail=f"{market.venue.value} order book unavailable")
        return {
            "venue": market.venue.value,
            "market_id": market.native_id,
            "books": {k.value: _book_json(v) for k, v in books.items()},
            "server_time": utcnow().isoformat(),
        }

    @app.post("/v1/proposals/analyze")
    async def analyze(body: AnalyzeBody, request: Request, _: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            market = await agent.get_market(body.market_id, body.venue)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)[:200]) from exc
        result = await agent.analyze_market(
            market, only_outcome=body.outcome, create_proposal=True, max_amount_usd=body.amount_usd
        )
        if body.execute and result.get("ok"):
            return await agent.approve(result["proposal"]["id"], actor=actor(request), via="ui")
        return result

    @app.get("/v1/proposals")
    async def proposals(limit: int = 50, _: str = Depends(require_auth)) -> dict[str, Any]:
        rows = agent.store.list_proposals(limit=min(max(limit, 1), 200))
        return {"proposals": [p.model_dump(mode="json") for p in rows]}

    @app.get("/v1/proposals/{proposal_id}")
    async def proposal_detail(proposal_id: str, _: str = Depends(require_auth)) -> dict[str, Any]:
        proposal = agent.store.get_proposal(proposal_id)
        if not proposal:
            raise HTTPException(status_code=404, detail="proposal not found")
        return proposal.model_dump(mode="json")

    @app.post("/v1/proposals/{proposal_id}/approve")
    async def approve(
        proposal_id: str,
        body: ApproveBody,
        request: Request,
        _: str = Depends(require_auth),
    ) -> dict[str, Any]:
        result = await agent.approve(proposal_id, actor=actor(request), via="ui", amount_usd=body.amount_usd)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=redact(result))
        return result

    @app.post("/v1/proposals/{proposal_id}/reject")
    async def reject(proposal_id: str, request: Request, _: str = Depends(require_auth)) -> dict[str, Any]:
        result = await agent.reject(proposal_id, actor=actor(request), via="ui")
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("error", "reject failed"))
        return result

    @app.get("/v1/positions")
    async def positions(_: str = Depends(require_auth)) -> dict[str, Any]:
        return {"positions": [p.model_dump(mode="json") for p in agent.positions()]}

    @app.post("/v1/account/sync")
    async def account_sync(venue: VenueId | None = None, _: str = Depends(require_auth)) -> dict[str, Any]:
        """Read the venue's own account now (positions, resting orders, balance) and update the app's copy.

        This is a read: it places nothing. Pull-to-refresh uses it so a trade made on the venue's website
        shows without waiting for the next sync.
        """

        targets = [venue] if venue else [v for v in registry.enabled() if registry.is_ready(v)]
        results: list[dict[str, Any]] = []
        for target in targets:
            if not registry.is_ready(target):
                results.append({"venue": target.value, "ok": False, "reason": registry.reason(target)})
                continue
            try:
                results.append({"ok": True, **await agent.account_sync.sync_once(target)})
            except Exception as exc:
                results.append(
                    {"venue": target.value, "ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:160]}"}
                )
        return {"results": results}

    @app.post("/v1/positions/close")
    async def close_position(body: CloseBody, request: Request, _: str = Depends(require_auth)) -> dict[str, Any]:
        result = await agent.close_position(
            body.market_id, body.outcome, actor=actor(request), amount_usd=body.amount_usd
        )
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result)
        return result

    @app.get("/v1/orders")
    async def orders(
        venue: VenueId | None = None,
        status: str | None = None,
        limit: int = 100,
        _: str = Depends(require_auth),
    ) -> dict[str, Any]:
        states = None
        if status == "open":
            states = {"OPEN", "PARTIAL"}
        elif status:
            states = {status.upper()}
        return {"orders": agent.store.list_orders(venue=venue, limit=min(max(limit, 1), 200), states=states)}

    @app.get("/v1/rules")
    async def rules(_: str = Depends(require_auth)) -> dict[str, Any]:
        return {"rules": agent.rules()}

    @app.post("/v1/rules")
    async def create_rule(body: RuleBody, request: Request, _: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            kind = body.kind.upper()
            from .domain import RuleKind

            rule_kind = RuleKind(kind)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid rule kind: {body.kind}") from exc
        result = agent.create_exit_rule(
            market_id=body.market_id,
            outcome=body.outcome,
            kind=rule_kind,
            trigger_price=body.trigger_price,
            trigger_at=body.trigger_at,
            actor=actor(request),
        )
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result)
        return result

    @app.delete("/v1/rules/{rule_id}")
    async def delete_rule(rule_id: str, request: Request, _: str = Depends(require_auth)) -> dict[str, Any]:
        return agent.cancel_rule(rule_id, actor=actor(request))

    @app.post("/v1/pause")
    async def pause(request: Request, _: str = Depends(require_auth)) -> dict[str, Any]:
        return await agent.set_paused(True, actor=actor(request))

    @app.post("/v1/resume")
    async def resume(request: Request, _: str = Depends(require_auth)) -> dict[str, Any]:
        return await agent.set_paused(False, actor=actor(request))

    @app.post("/v1/panic")
    async def panic(body: PanicBody, request: Request, _: str = Depends(require_auth)) -> dict[str, Any]:
        if not body.confirm:
            return {
                "ok": False,
                "confirm_required": True,
                "message": "Re-send with confirm=true to pause, cancel all venue orders and cancel all rules.",
            }
        return await agent.panic(actor=actor(request), confirm=True)

    @app.post("/v1/limits")
    async def set_limit(body: LimitBody, request: Request, _: str = Depends(require_auth)) -> dict[str, Any]:
        result = agent.set_limit(body.name, body.value, actor=actor(request))
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("error", "limit rejected"))
        return result

    # ── full control: capabilities, balance, step-up, ceilings ──────

    def _session_id(request: Request, user: str) -> str:
        """A stable id for the session, so a step-up token binds to one session."""

        token = request.headers.get("authorization", "")
        if token.lower().startswith("bearer "):
            return _hash_token(token.split(" ", 1)[1].strip())
        cookie = request.cookies.get(SESSION_COOKIE)
        return _hash_token(cookie or user)

    def _hash_token(value: str) -> str:
        import hashlib

        return hashlib.sha256(value.encode()).hexdigest()[:32]

    def _idempotency_key(request: Request) -> str | None:
        return request.headers.get("idempotency-key") or request.headers.get("x-idempotency-key")

    def _fail(exc: FullControlError | FundsError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"ok": False, "error": {"code": exc.code, "message": str(exc)}},
        )

    @app.get("/v1/capabilities")
    async def capabilities(_: str = Depends(require_auth)) -> dict[str, Any]:
        return full.capabilities()

    @app.get("/v1/balance")
    async def balance(venue: VenueId | None = None, _: str = Depends(require_auth)) -> dict[str, Any]:
        return await full.balance(venue)

    @app.post("/v1/auth/stepup")
    async def stepup(body: StepupBody, request: Request, user: str = Depends(require_auth)) -> Any:
        client = request.client.host if request.client else "unknown"
        now = time.time()
        bucket = login_attempts[f"stepup:{client}"]
        while bucket and bucket[0] < now - 300:
            bucket.popleft()
        if len(bucket) >= 8:
            raise HTTPException(status_code=429, detail="too many step-up attempts; wait 5 minutes")
        bucket.append(now)
        try:
            return full.create_stepup(body.password, _session_id(request, user), body.action)
        except FullControlError as exc:
            return _fail(exc)

    @app.post("/v1/limits/hard-ceiling")
    async def set_hard_ceiling(
        body: CeilingBody,
        request: Request,
        user: str = Depends(require_auth),
        x_stepup: str | None = Header(None, alias="X-Stepup-Token"),
    ) -> Any:
        session = _session_id(request, user)
        try:
            current = agent.risk.ceilings.get(body.name)
            if current is None:
                raise FullControlError(f"unknown hard ceiling {body.name}", code="unknown_ceiling", status=400)
            raising = body.value > current
            if raising:
                full.require_stepup(x_stepup, session, "ceiling_raise")
            return full.set_hard_ceiling(body.name, body.value, actor=actor(request), raise_verified=raising)
        except FullControlError as exc:
            return _fail(exc)

    # ── full control: manual trading ───────────────────────────────

    @app.post("/v1/orders/limit")
    async def place_limit_order(
        body: LimitOrderBody,
        request: Request,
        _: str = Depends(require_auth),
    ) -> Any:
        payload = body.model_dump(mode="json")
        try:
            stored, request_hash = full.idempotent(_idempotency_key(request), "orders.limit", payload)
            if stored is not None:
                return {"ok": True, "replayed": True, **stored}
            result = await full.place_manual_order(
                venue=body.venue,
                market_id=body.market_id,
                outcome=body.outcome,
                side=body.side,
                price=body.price,
                shares=body.shares,
                amount_usd=body.amount_usd,
                order_type=body.tif,
                expires_at=body.expires_at,
                post_only=body.post_only,
                client_id=body.client_id,
                actor=actor(request),
            )
        except FullControlError as exc:
            return _fail(exc)
        full.remember(_idempotency_key(request) or "", "orders.limit", request_hash, result)
        return {"ok": True, **result}

    @app.post("/v1/orders/market")
    async def place_market_order(
        body: MarketOrderBody,
        request: Request,
        _: str = Depends(require_auth),
    ) -> Any:
        payload = body.model_dump(mode="json")
        try:
            stored, request_hash = full.idempotent(_idempotency_key(request), "orders.market", payload)
            if stored is not None:
                return {"ok": True, "replayed": True, **stored}
            result = await full.place_manual_order(
                venue=body.venue,
                market_id=body.market_id,
                outcome=body.outcome,
                side=body.side,
                price=None,
                shares=body.shares,
                amount_usd=body.amount_usd,
                order_type=OrderType.FAK,
                client_id=body.client_id,
                actor=actor(request),
                max_slippage_cents=body.max_slippage_cents,
            )
        except FullControlError as exc:
            return _fail(exc)
        full.remember(_idempotency_key(request) or "", "orders.market", request_hash, result)
        return {"ok": True, **result}

    @app.post("/v1/positions/sell")
    async def sell_position(body: SellBody, request: Request, _: str = Depends(require_auth)) -> Any:
        payload = body.model_dump(mode="json")
        try:
            stored, request_hash = full.idempotent(_idempotency_key(request), "positions.sell", payload)
            if stored is not None:
                return {"ok": True, "replayed": True, **stored}
            result = await full.sell_position(
                venue=body.venue,
                market_id=body.market_id,
                outcome=body.outcome,
                shares=body.shares,
                price=body.price,
                actor=actor(request),
            )
        except FullControlError as exc:
            return _fail(exc)
        full.remember(_idempotency_key(request) or "", "positions.sell", request_hash, result)
        return {"ok": True, **result}

    @app.get("/v1/orders/{order_id}")
    async def order_detail(order_id: str, _: str = Depends(require_auth)) -> Any:
        order = agent.store.get_order(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="order not found")
        return {"order": order, "fills": agent.store.fills_for_order(order["id"])}

    @app.post("/v1/orders/{order_id}/cancel")
    async def cancel_order_by_id(order_id: str, request: Request, _: str = Depends(require_auth)) -> Any:
        """The original route shape is kept; the body is additive."""

        try:
            result = await agent.execution.cancel(order_id, actor=actor(request))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not result.get("ok"):
            return JSONResponse(
                status_code=409,
                content={"ok": False, "error": {"code": "cancel_failed", "message": result.get("message", "")}},
            )
        return {"ok": True, **result}

    @app.post("/v1/orders/cancel-all")
    async def cancel_all_orders(body: CancelAllBody, request: Request, _: str = Depends(require_auth)) -> Any:
        """The kill switch. Idempotent and never blocked by caps."""

        try:
            stored, request_hash = full.idempotent(
                _idempotency_key(request), "orders.cancel_all", body.model_dump(mode="json")
            )
            if stored is not None:
                return {"ok": True, "replayed": True, **stored}
            result = await full.cancel_all_orders(venue=body.venue, market_id=body.market_id, actor=actor(request))
        except FullControlError as exc:
            return _fail(exc)
        full.remember(_idempotency_key(request) or "", "orders.cancel_all", request_hash, result)
        return {"ok": True, **result}

    @app.post("/v1/orders/{order_id}/amend")
    async def amend_order(order_id: str, body: AmendBody, request: Request, _: str = Depends(require_auth)) -> Any:
        try:
            result = await full.amend_order(order_id, price=body.price, shares=body.shares, actor=actor(request))
        except FullControlError as exc:
            return _fail(exc)
        return {"ok": True, **result}

    # ── full control: market data and history ─────────────────────

    @app.get("/v1/markets/{market_id}/book")
    async def market_book(
        market_id: str,
        venue: VenueId | None = None,
        outcome: Outcome | None = None,
        depth: int = 20,
        _: str = Depends(require_auth),
    ) -> Any:
        try:
            return await full.market_book(market_id, venue=venue, outcome=outcome, depth=min(max(depth, 1), 100))
        except FullControlError as exc:
            return _fail(exc)

    @app.get("/v1/markets/{market_id}/history")
    async def market_history(
        market_id: str,
        venue: VenueId | None = None,
        interval: str = "1d",
        outcome: Outcome = Outcome.YES,
        _: str = Depends(require_auth),
    ) -> Any:
        try:
            return await full.market_history(market_id, venue=venue, interval=interval, outcome=outcome)
        except FullControlError as exc:
            return _fail(exc)

    @app.get("/v1/trades")
    async def trades(
        venue: VenueId | None = None,
        market_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        _: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return full.trades(venue=venue, market_id=market_id, limit=min(max(limit, 1), 500), cursor=cursor)

    @app.get("/v1/pnl/daily")
    async def pnl_daily(days: int = 30, venue: VenueId | None = None, _: str = Depends(require_auth)) -> dict[str, Any]:
        return full.pnl_daily(min(max(days, 1), 365), venue=venue)

    @app.post("/v1/positions/redeem")
    async def redeem(body: RedeemBody, request: Request, _: str = Depends(require_auth)) -> Any:
        try:
            return {
                "ok": True,
                **await full.redeem(
                    venue=body.venue, market_id=body.market_id, outcome=body.outcome, actor=actor(request)
                ),
            }
        except FullControlError as exc:
            return _fail(exc)

    @app.post("/v1/positions/split")
    async def split_position(body: RedeemBody, request: Request, _: str = Depends(require_auth)) -> Any:
        try:
            return {
                "ok": True,
                **await full.split_merge(
                    venue=body.venue, market_id=body.market_id, action="split", actor=actor(request)
                ),
            }
        except FullControlError as exc:
            return _fail(exc)

    @app.post("/v1/positions/merge")
    async def merge_position(body: RedeemBody, request: Request, _: str = Depends(require_auth)) -> Any:
        try:
            return {
                "ok": True,
                **await full.split_merge(
                    venue=body.venue, market_id=body.market_id, action="merge", actor=actor(request)
                ),
            }
        except FullControlError as exc:
            return _fail(exc)

    # ── full control: funds ────────────────────────────────────────

    @app.get("/v1/funds/{venue}/deposit/options")
    async def deposit_options(venue: VenueId, _: str = Depends(require_auth)) -> Any:
        """Every chain and token the venue accepts for a deposit (Polymarket: the bridge's own list)."""
        try:
            options = await funds.deposit_options(venue)
        except FundsError as exc:
            return _fail(exc)
        return {"ok": True, **options}

    @app.get("/v1/funds/{venue}/withdraw/options")
    async def withdraw_options(venue: VenueId, _: str = Depends(require_auth)) -> Any:
        """Chains and tokens a withdrawal can go to (Polymarket: the bridge's own list, EVM chains only)."""
        try:
            options = await funds.withdraw_options(venue)
        except FundsError as exc:
            return _fail(exc)
        return {"ok": True, "require_allowlist": bool(settings.funds_require_allowlist), **options}

    @app.get("/v1/funds/{venue}/deposit")
    async def deposit_info(
        venue: VenueId,
        network: str | None = None,
        asset: str | None = None,
        token: str | None = None,
        _: str = Depends(require_auth),
    ) -> Any:
        try:
            info = await funds.deposit_info(venue, network=network, asset=asset, token=token)
        except FundsError as exc:
            return _fail(exc)
        return {"ok": True, **info.as_dict()}

    @app.get("/v1/funds/allowlist")
    async def allowlist_list(_: str = Depends(require_auth)) -> dict[str, Any]:
        return funds.allowlist_list()

    @app.post("/v1/funds/allowlist")
    async def allowlist_add(
        body: AllowlistBody,
        request: Request,
        user: str = Depends(require_auth),
        x_stepup: str | None = Header(None, alias="X-Stepup-Token"),
    ) -> Any:
        try:
            full.require_stepup(x_stepup, _session_id(request, user), "allowlist_add")
            return {
                "ok": True,
                **funds.allowlist_add(
                    label=body.label,
                    address=body.address,
                    network=body.network,
                    asset=body.asset,
                    actor=actor(request),
                ),
            }
        except (FundsError, FullControlError) as exc:
            return _fail(exc)

    @app.delete("/v1/funds/allowlist/{entry_id}")
    async def allowlist_remove(entry_id: str, request: Request, _: str = Depends(require_auth)) -> Any:
        try:
            return {"ok": True, **funds.allowlist_remove(entry_id, actor=actor(request))}
        except FundsError as exc:
            return _fail(exc)

    @app.post("/v1/funds/withdraw/preview")
    async def withdraw_preview(body: WithdrawPreviewBody, _: str = Depends(require_auth)) -> Any:
        try:
            return {
                "ok": True,
                **await funds.preview_withdraw(
                    venue=body.venue,
                    amount=body.amount,
                    address_id=body.address_id,
                    recipient=body.recipient,
                    network=body.network,
                    asset=body.asset,
                ),
            }
        except FundsError as exc:
            return _fail(exc)

    @app.post("/v1/funds/withdraw")
    async def create_withdrawal(
        body: WithdrawBody,
        request: Request,
        user: str = Depends(require_auth),
    ) -> Any:
        payload = body.model_dump(mode="json")
        try:
            stored, request_hash = full.idempotent(_idempotency_key(request), "funds.withdraw", payload)
            if stored is not None:
                return {"ok": True, "replayed": True, **stored}
            result = await funds.create_withdrawal(
                venue=body.venue,
                amount=body.amount,
                address_id=body.address_id,
                recipient=body.recipient,
                network=body.network,
                asset=body.asset,
                actor=actor(request),
            )
        except (FundsError, FullControlError) as exc:
            return _fail(exc)
        full.remember(_idempotency_key(request) or "", "funds.withdraw", request_hash, result)
        return {"ok": True, **result}

    @app.get("/v1/funds/withdrawals")
    async def list_withdrawals(venue: VenueId | None = None, _: str = Depends(require_auth)) -> dict[str, Any]:
        return funds.list_withdrawals(venue=venue)

    @app.post("/v1/funds/withdrawals/{withdrawal_id}/cancel")
    async def cancel_withdrawal(withdrawal_id: str, request: Request, _: str = Depends(require_auth)) -> Any:
        try:
            return {"ok": True, **funds.cancel_withdrawal(withdrawal_id, actor=actor(request))}
        except FundsError as exc:
            return _fail(exc)

    @app.get("/v1/funds/history")
    async def funds_history(venue: VenueId | None = None, _: str = Depends(require_auth)) -> dict[str, Any]:
        """Venue-reported deposits and withdrawals. Read-only everywhere."""

        if not funds.enabled:
            return {"ok": False, "history": [], "reason": "the funds module is off"}
        venues = [venue] if venue else list(registry.enabled())
        rows: list[dict[str, Any]] = []
        for target in venues:
            if not registry.get(target).caps.funds_history:
                continue
            try:
                if target is VenueId.POLYMARKET:
                    wallet = await polymarket_signing.resolve_wallet(settings.polymarket_wallet_address)
                    if wallet:
                        for row in await funds.polymarket_history(wallet):
                            rows.append({"venue": target.value, **row})
                    continue
                for row in await registry.get(target).get_funds_history():
                    rows.append({"venue": target.value, **row})
            except Exception:
                continue
        return {"ok": True, "history": rows}

    @app.get("/v1/approvals")
    async def approvals(venue: VenueId | None = None, _: str = Depends(require_auth)) -> dict[str, Any]:
        venues = [venue] if venue else list(registry.enabled())
        out: list[dict[str, Any]] = []
        for target in venues:
            adapter = registry.get(target)
            out.append(
                {
                    "venue": target.value,
                    "needed": adapter.caps.approvals_needed,
                    "reason": adapter.caps.unsupported_reason.get("approvals", ""),
                    "official_url": "https://docs.polymarket.com/trading/wallets-auth",
                }
            )
        return {"approvals": out}

    @app.post("/v1/approvals")
    async def set_approvals(
        body: ApprovalBody,
        request: Request,
        user: str = Depends(require_auth),
        x_stepup: str | None = Header(None, alias="X-Stepup-Token"),
    ) -> Any:
        try:
            full.require_stepup(x_stepup, _session_id(request, user), "approvals")
            adapter = registry.get(body.venue)
            if not adapter.caps.approvals_needed:
                raise FullControlError(
                    f"{body.venue.value} does not need on-chain approvals for trading",
                    code="no_approvals_needed",
                    status=400,
                )
            raise FullControlError(
                f"{body.venue.value} approvals are not automated; grant them on the venue site "
                "after checking the exact spender and amount",
                code="approvals_manual",
                status=400,
            )
        except FullControlError as exc:
            return _fail(exc)

    @app.get("/v1/audit")
    async def audit_log(_: str = Depends(require_auth)) -> dict[str, Any]:
        return {"entries": agent.store.recent_audit(200)}

    @app.get("/v1/reports/daily")
    async def daily_report_endpoint(_: str = Depends(require_auth)) -> dict[str, Any]:
        from .reports import daily_report, render_report

        report = daily_report(agent.store, agent)
        return {"report": report, "text": render_report(report)}

    def own_conversation(user: str, conversation_id: str) -> dict[str, Any]:
        """The chat if it belongs to this user; otherwise a 404 that looks the same as 'no such chat'."""

        try:
            return agent.store.chat_get(f"web:{user}", conversation_id)
        except ConversationNotFound:
            raise HTTPException(status_code=404, detail="conversation not found") from None

    @app.post("/v1/chat")
    async def chat_message(body: ChatBody, request: Request, user: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return await chat.handle(
                body.message, actor=f"web:{user}", source="web_chat", conversation_id=body.conversation_id
            )
        except ConversationNotFound:
            raise HTTPException(status_code=404, detail="conversation not found") from None
        except ConversationBusy:
            raise HTTPException(status_code=409, detail="this chat is still answering its previous message") from None

    def chat_event_stream(message: str, user: str, conversation_id: str | None = None) -> StreamingResponse:
        if conversation_id is not None and own_conversation(user, conversation_id)["status"] == "running":
            raise HTTPException(status_code=409, detail="this chat is still answering its previous message")

        async def event_stream() -> Any:
            yield 'event: status\ndata: {"state":"working"}\n\n'

            queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
            stop = {"now": False}

            async def on_event(kind: str, payload: dict[str, Any]) -> None:
                queue.put_nowait((kind, payload))

            task = asyncio.create_task(
                chat.handle(
                    message,
                    actor=f"web:{user}",
                    source="web_chat",
                    on_event=on_event,
                    should_stop=lambda: stop["now"],
                    conversation_id=conversation_id,
                )
            )
            # Keep a reference: if the app disconnects, the task still ends cleanly.
            _CHAT_TASKS.add(task)
            task.add_done_callback(_CHAT_TASKS.discard)
            getter: asyncio.Future[tuple[str, dict[str, Any]]] | None = None
            try:
                while True:
                    if task.done() and queue.empty():
                        break
                    getter = asyncio.ensure_future(queue.get())
                    await asyncio.wait({getter, task}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
                    if getter.done():
                        kind, payload = getter.result()
                        yield f"event: {kind}\ndata: " + json.dumps(redact(payload), default=str) + "\n\n"
                    else:
                        getter.cancel()
                        if not task.done():
                            yield "event: ping\ndata: {}\n\n"
                try:
                    result = task.result()
                except ConversationBusy:  # lost a race with another message to the same chat
                    result = {"reply": "This chat is still answering its previous message.", "data": {"error": True}}
                except ConversationNotFound:
                    result = {"reply": "This chat no longer exists.", "data": {"error": True}}
                except Exception as exc:
                    result = {"reply": f"Chat failed: {type(exc).__name__}", "data": {}}
                yield "event: message\ndata: " + json.dumps(redact(result), default=str) + "\n\n"
                yield "event: done\ndata: {}\n\n"
            finally:
                if getter is not None and not getter.done():
                    getter.cancel()
                # The app pressed Stop (or lost the connection). Never cancel the task: a trade
                # that already started must finish. It just may not start anything new.
                if not task.done():
                    stop["now"] = True

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/chat/stream")
    async def chat_stream(
        message: str, request: Request, conversation_id: str | None = None, user: str = Depends(require_auth)
    ) -> StreamingResponse:
        return chat_event_stream(message, user, conversation_id)

    # Same stream with the message in the body: a long or non-Latin message does not fit in a URL,
    # and the text stays out of access logs. The Android app uses this form.
    @app.post("/v1/chat/stream")
    async def chat_stream_post(
        body: ChatBody, request: Request, user: str = Depends(require_auth)
    ) -> StreamingResponse:
        return chat_event_stream(body.message, user, body.conversation_id)

    # ----------------------------------------------------------- conversations
    # Every route is scoped to the signed-in user in the store itself; see Store._owned.
    @app.get("/v1/conversations")
    async def conversations_list(
        q: str | None = None, limit: int = 100, user: str = Depends(require_auth)
    ) -> dict[str, Any]:
        return {"conversations": agent.store.chat_list(f"web:{user}", q, limit)}

    @app.post("/v1/conversations")
    async def conversations_create(body: ConversationBody, user: str = Depends(require_auth)) -> dict[str, Any]:
        return {"conversation": agent.store.chat_create(f"web:{user}", body.title)}

    @app.get("/v1/conversations/{conversation_id}")
    async def conversations_open(conversation_id: str, user: str = Depends(require_auth)) -> dict[str, Any]:
        """Everything needed to restore a chat: its summary, run state and the latest messages."""

        conversation = own_conversation(user, conversation_id)
        messages = agent.store.chat_messages(f"web:{user}", conversation_id, limit=CHAT_PAGE_SIZE)
        return {
            "conversation": conversation,
            "messages": messages,
            "has_more": bool(messages) and messages[0]["seq"] > 1,
        }

    @app.get("/v1/conversations/{conversation_id}/messages")
    async def conversation_messages(
        conversation_id: str, before: int | None = None, limit: int = CHAT_PAGE_SIZE, user: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One page of older history: the [limit] messages before sequence number [before]."""

        own_conversation(user, conversation_id)
        messages = agent.store.chat_messages(f"web:{user}", conversation_id, before=before, limit=limit)
        return {"messages": messages, "has_more": bool(messages) and messages[0]["seq"] > 1}

    @app.post("/v1/conversations/{conversation_id}/rename")
    async def conversations_rename(
        conversation_id: str, body: ConversationBody, user: str = Depends(require_auth)
    ) -> dict[str, Any]:
        own_conversation(user, conversation_id)
        if not body.title.strip():
            raise HTTPException(status_code=422, detail="a title is required")
        return {"conversation": agent.store.chat_rename(f"web:{user}", conversation_id, body.title)}

    @app.delete("/v1/conversations/{conversation_id}")
    async def conversations_delete(conversation_id: str, user: str = Depends(require_auth)) -> dict[str, Any]:
        own_conversation(user, conversation_id)
        agent.store.chat_delete(f"web:{user}", conversation_id)
        return {"deleted": True}

    @app.websocket("/v1/ws")
    async def websocket(ws: WebSocket) -> None:
        # The native app cannot rely on a browser cookie, so a Bearer token works too.
        token = ws.cookies.get(SESSION_COOKIE)
        if not token:
            header = ws.headers.get("authorization", "")
            if header.lower().startswith("bearer "):
                token = header.split(" ", 1)[1].strip()
        if not token or _username_from_token(token) is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        await ws.send_json({"topic": "hello", "payload": {"badge": _badge()}, "at": utcnow().isoformat()})
        try:
            async with bus.subscribe() as stream:
                async for event in stream:
                    await ws.send_json(event.as_dict())
        except WebSocketDisconnect:
            return
        except Exception:
            return

    @app.websocket("/v1/stream")
    async def stream(ws: WebSocket) -> None:
        """Push updates for event views. Same auth as ``/v1/ws``. Client frames: ``{"op": "subscribe" |
        "unsubscribe" | "snapshot", "topic": "event", "venue", "id", "range"}``. Server frames: ``snapshot``,
        ``patch``, ``status``, ``error``. A gap in ``seq`` means ask for a snapshot. Display data only."""

        token = ws.cookies.get(SESSION_COOKIE)
        if not token:
            header = ws.headers.get("authorization", "")
            if header.lower().startswith("bearer "):
                token = header.split(" ", 1)[1].strip()
        user = _username_from_token(token) if token else None
        if user is None:
            await ws.close(code=4401)
            return
        if not settings.live_views_enabled:
            await ws.close(code=4404)
            return
        await ws.accept()
        if own_hub:
            live_hub.start()
        sink = live_hub.connect(user)
        await ws.send_json({"type": "hello", "topic": "event", "server_ts": utcnow().isoformat()})

        async def pump() -> None:
            while (frame := await sink.get()) is not None:
                await ws.send_json(frame)
            await ws.close(code=4408)  # the hub dropped this client (it stopped reading)

        sender = asyncio.create_task(pump())
        try:
            async for raw in ws.iter_text():
                try:
                    msg = json.loads(raw) if len(raw) <= 2048 else None
                except ValueError:
                    msg = None
                if not isinstance(msg, dict):
                    sink.put({"type": "error", "code": "bad_frame", "detail": "send a small JSON object"})
                    continue
                await live_hub.handle(sink, msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            live_hub.disconnect(sink)
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    @app.head("/", include_in_schema=False)
    async def index_head() -> Response:
        # Hosting probes (Render's port check) send HEAD / and log a 405 without this.
        return Response(status_code=200)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> Any:
        if not STATIC.exists():
            return HTMLResponse("<h1>UI not built</h1>", status_code=500)
        return FileResponse(STATIC / "index.html", media_type="text/html")

    if STATIC.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    def _badge() -> str:
        active = registry.active_or_none
        if active is None:
            return "NO VENUE READY"
        return f"{active.value.upper()} | LIVE"

    return app
