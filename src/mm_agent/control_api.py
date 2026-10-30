"""FastAPI Control API, session auth, WebSocket events and SSE chat."""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections import defaultdict, deque
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
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

from .agent import AgentService
from .chat import ChatService
from .config import Settings
from .domain import Outcome, VenueId, utcnow
from .events import EventBus
from .scanner import Scanner
from .util import get_logger, redact
from .venues.registry import VenueRegistry

logger = get_logger(__name__)
STATIC = Path(__file__).parent / "static"
SESSION_COOKIE = "mm_session"


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


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class LimitBody(BaseModel):
    name: str
    value: str


class PanicBody(BaseModel):
    confirm: bool = False


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
) -> FastAPI:
    serializer = URLSafeTimedSerializer(settings.session_secret or "unset-secret", salt="mm-session")
    login_attempts: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=10))
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
        return {"status": "ok", "time": utcnow().isoformat(), "version": "0.1.0"}

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
        token = serializer.dumps({"u": settings.admin_username, "v": "1"})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=settings.session_ttl_hours * 3600,
            httponly=True,
            samesite="strict",
            secure=settings.app_env.lower() in {"prod", "production"},
            path="/",
        )
        agent.store.audit(f"web:{settings.admin_username}", "auth.login", {})
        return {"ok": True, "username": settings.admin_username, "token": token}

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

    @app.get("/v1/status")
    async def get_status(_: str = Depends(require_auth)) -> dict[str, Any]:
        return agent.status()

    @app.get("/status")
    async def get_status_short(_: str = Depends(require_auth)) -> dict[str, Any]:
        return agent.status()

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
            "books": {k.value: v.model_dump(mode="json") for k, v in books.items()},
            "research": research,
            "badge": _badge(),
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

    @app.post("/v1/positions/close")
    async def close_position(body: CloseBody, request: Request, _: str = Depends(require_auth)) -> dict[str, Any]:
        result = await agent.close_position(
            body.market_id, body.outcome, actor=actor(request), amount_usd=body.amount_usd
        )
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result)
        return result

    @app.get("/v1/orders")
    async def orders(limit: int = 100, _: str = Depends(require_auth)) -> dict[str, Any]:
        return {"orders": agent.store.list_orders(limit=min(max(limit, 1), 200))}

    @app.post("/v1/orders/{order_id}/cancel")
    async def cancel_order(order_id: str, request: Request, _: str = Depends(require_auth)) -> dict[str, Any]:
        result = await agent.execution.cancel(order_id, actor=actor(request))
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result)
        return result

    @app.post("/v1/orders/cancel-all")
    async def cancel_all(request: Request, _: str = Depends(require_auth)) -> dict[str, Any]:
        return {"results": await agent.execution.cancel_all(actor=actor(request))}

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

    @app.get("/v1/audit")
    async def audit_log(_: str = Depends(require_auth)) -> dict[str, Any]:
        return {"entries": agent.store.recent_audit(200)}

    @app.get("/v1/reports/daily")
    async def daily_report_endpoint(_: str = Depends(require_auth)) -> dict[str, Any]:
        from .reports import daily_report, render_report

        report = daily_report(agent.store, agent)
        return {"report": report, "text": render_report(report)}

    @app.post("/v1/chat")
    async def chat_message(body: ChatBody, request: Request, user: str = Depends(require_auth)) -> dict[str, Any]:
        return await chat.handle(body.message, actor=f"web:{user}", source="web_chat")

    @app.get("/v1/chat/stream")
    async def chat_stream(message: str, request: Request, user: str = Depends(require_auth)) -> StreamingResponse:
        async def event_stream() -> Any:
            yield 'event: status\ndata: {"state":"working"}\n\n'

            async def run() -> dict[str, Any]:
                return await chat.handle(message, actor=f"web:{user}", source="web_chat")

            task = asyncio.create_task(run())
            while not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=10)
                except TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
            try:
                result = task.result()
            except Exception as exc:
                result = {"reply": f"Chat failed: {type(exc).__name__}", "data": {}}
            yield "event: message\ndata: " + json.dumps(redact(result), default=str) + "\n\n"
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.websocket("/v1/ws")
    async def websocket(ws: WebSocket) -> None:
        token = ws.cookies.get(SESSION_COOKIE)
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
            return "NO VENUE | PAPER"
        return f"{active.value.upper()} | {registry.mode(active).value}"

    return app
