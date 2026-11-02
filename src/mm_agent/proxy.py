"""Outbound proxy for Polymarket traffic only, with ordered fallback.

Every request to a ``*.polymarket.com`` host (REST, the official SDK and WebSockets) is tried in this order:
the server's own IP first (direct, as before), then the proxies of the list. When the direct connection fails or is
blocked, the next proxy is tried, and so on. Everything else (Kalshi, Limitless, the LLM, Coinbase, the Polygon
RPC, ...) is sent directly and never touches a proxy.

Configuration (all optional):

* ``POLYMARKET_PROXY_URLS``: comma, space or newline separated proxy URLs, in priority order, e.g.
  ``http://user:pass@host:port,http://host2:port``. When set it REPLACES the built-in public proxies, so this is
  where a private proxy goes.
* ``POLYMARKET_PROXY_ENABLED``: ``false`` turns the whole thing off (direct connections).
* ``POLYMARKET_PROXY_COOLDOWN_SECONDS``: how long a failed proxy is tried last (default 60).
* ``POLYMARKET_PROXY_DIRECT_FIRST``: ``true`` (default) tries the direct connection before the proxies; ``false``
  puts the proxies first.
* ``POLYMARKET_PROXY_DIRECT_FALLBACK``: only used when direct-first is off: ``true`` goes direct when every proxy
  failed.

Only failures that happen BEFORE the request is accepted (cannot connect, proxy refused the tunnel, an HTTP 403/451
block page) move on to the next route for any method, so an order is never sent twice. A read timeout moves on only
for GET/HEAD. A route that failed is tried last for the cooldown, so a blocked server IP does not slow every call.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import httpx

from .util import get_logger

log = get_logger("proxy")

# Public, free proxies: slow, short-lived and shared with strangers. Replace them with a private proxy through
# POLYMARKET_PROXY_URLS. HTTPS traffic is tunnelled (CONNECT), so a proxy sees the host name but not the content.
PUBLIC_PROXIES: tuple[str, ...] = (
    "http://157.66.16.63:8181",
    "http://103.133.27.239:8080",
    "http://202.51.106.229:8080",
)

PROXIED_SUFFIXES: tuple[str, ...] = ("polymarket.com",)

_CONNECT_ERRORS: tuple[type[BaseException], ...] = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)
_IDEMPOTENT_ERRORS: tuple[type[BaseException], ...] = (
    *_CONNECT_ERRORS,
    httpx.ReadTimeout,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

DIRECT = "direct"  # the server's own IP; sits in the route list like a proxy URL
BLOCKED_STATUSES = frozenset({403, 451})  # geo-block / edge refusal: the request was not processed


def parse_proxy_list(raw: str) -> list[str]:
    """Split a comma/space/newline list, add ``http://`` to bare ``host:port`` entries and drop duplicates."""

    out: list[str] = []
    for item in re.split(r"[\s,;]+", raw or ""):
        item = item.strip()
        if not item:
            continue
        if "://" not in item:
            item = "http://" + item
        if item not in out:
            out.append(item)
    return out


def redact(url: str) -> str:
    """``scheme://host:port`` without credentials, safe for logs."""

    if url == DIRECT:
        return DIRECT
    parts = urlsplit(url)
    host = parts.hostname or "?"
    return f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")


def is_proxied_host(host: str | None) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in PROXIED_SUFFIXES)


class ProxyPool:
    """Ordered route list (direct and/or proxies). A failed route is moved to the back for ``cooldown`` seconds,
    never dropped."""

    def __init__(
        self,
        urls: Sequence[str],
        *,
        cooldown: float = 60.0,
        direct_fallback: bool = False,
        direct_first: bool = False,
        enabled: bool = True,
    ) -> None:
        self.urls = list(urls)
        self.cooldown = cooldown
        self.direct_fallback = direct_fallback
        self.direct_first = direct_first
        self.enabled = enabled and bool(self.urls)
        self._failed_at: dict[str, float] = {}

    def routes(self) -> list[str]:
        """Every route in configured priority order: ``DIRECT`` first, last or absent, then the proxies."""

        if self.direct_first:
            return [DIRECT, *self.urls]
        if self.direct_fallback:
            return [*self.urls, DIRECT]
        return list(self.urls)

    def candidates(self) -> list[str]:
        """Routes in priority order; the ones that failed recently come last."""

        now = time.monotonic()
        routes = self.routes()
        fresh = [u for u in routes if now - self._failed_at.get(u, -1e9) >= self.cooldown]
        cooling = [u for u in routes if u not in fresh]
        return fresh + cooling

    def mark_failed(self, url: str) -> None:
        self._failed_at[url] = time.monotonic()
        log.warning("proxy_failed", proxy=redact(url))  # "direct" when the server IP itself failed

    def mark_ok(self, url: str) -> None:
        self._failed_at.pop(url, None)


class PolymarketProxyTransport(httpx.AsyncBaseTransport):
    """httpx transport: Polymarket hosts through the proxy list with fallback, other hosts direct."""

    def __init__(self, pool: ProxyPool, *, limits: httpx.Limits | None = None) -> None:
        self._pool = pool
        self._limits = limits or httpx.Limits(max_connections=100, max_keepalive_connections=20)
        self._direct = httpx.AsyncHTTPTransport(limits=self._limits)
        self._proxied: dict[str, httpx.AsyncHTTPTransport] = {}

    def _transport_for(self, proxy_url: str) -> httpx.AsyncHTTPTransport:
        if proxy_url == DIRECT:
            return self._direct
        transport = self._proxied.get(proxy_url)
        if transport is None:
            transport = httpx.AsyncHTTPTransport(proxy=httpx.Proxy(proxy_url), limits=self._limits)
            self._proxied[proxy_url] = transport
        return transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self._pool.enabled or not is_proxied_host(request.url.host):
            return await self._direct.handle_async_request(request)
        retry_on = _IDEMPOTENT_ERRORS if request.method.upper() in _IDEMPOTENT_METHODS else _CONNECT_ERRORS
        last: BaseException | None = None
        blocked: httpx.Response | None = None
        for route in self._pool.candidates():
            try:
                response = await self._transport_for(route).handle_async_request(request)
            except retry_on as exc:
                self._pool.mark_failed(route)
                last = exc
                continue
            if response.status_code in BLOCKED_STATUSES:
                # refused at the edge (geo-block): nothing was processed, so the next route is safe for any method
                self._pool.mark_failed(route)
                if blocked is not None:
                    await blocked.aclose()
                blocked = response
                continue
            self._pool.mark_ok(route)
            if blocked is not None:
                await blocked.aclose()
            return response
        if blocked is not None:
            return blocked  # every route was refused: give the caller the real 403/451 instead of a made-up error
        assert last is not None
        raise last

    async def aclose(self) -> None:
        await self._direct.aclose()
        for transport in self._proxied.values():
            await transport.aclose()


# ------------------------------------------------------------------------------------------------- shared pool
_pool: ProxyPool | None = None


def get_pool() -> ProxyPool:
    global _pool
    if _pool is None:
        from .config import get_settings

        s = get_settings()
        urls = parse_proxy_list(s.polymarket_proxy_urls) or list(PUBLIC_PROXIES)
        _pool = ProxyPool(
            urls,
            cooldown=s.polymarket_proxy_cooldown_seconds,
            direct_fallback=s.polymarket_proxy_direct_fallback,
            direct_first=s.polymarket_proxy_direct_first,
            enabled=s.polymarket_proxy_enabled,
        )
        log.info("proxy_pool", enabled=_pool.enabled, routes=[redact(u) for u in _pool.routes()])
    return _pool


def reset_pool(pool: ProxyPool | None = None) -> None:
    """Tests, or a settings reload."""

    global _pool
    _pool = pool


def build_transport(*, limits: httpx.Limits | None = None) -> httpx.AsyncBaseTransport:
    """Transport for an ``httpx.AsyncClient`` that talks to Polymarket (and maybe other hosts)."""

    return PolymarketProxyTransport(get_pool(), limits=limits)


# ------------------------------------------------------------------------------------------------- websockets
class _FallbackConnect:
    """Drop-in for ``websockets.asyncio.client.connect(...)``: ``async with`` / ``await`` / ``async for`` all work
    for the single-connection forms the feeds use. Polymarket hosts go through the proxy list with fallback."""

    def __init__(self, url: str, **kwargs: Any) -> None:
        self._url = url
        self._kwargs = kwargs
        self._conn: Any = None

    async def _open(self) -> Any:
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidHandshake, InvalidProxy

        pool = get_pool()
        if not pool.enabled or not is_proxied_host(urlsplit(self._url).hostname):
            self._conn = connect(self._url, proxy=None, **self._kwargs)
            return await self._conn.__aenter__()
        last: BaseException | None = None
        for route in pool.candidates():
            conn = connect(self._url, proxy=None if route == DIRECT else route, **self._kwargs)
            try:
                ws = await conn.__aenter__()
            except (OSError, TimeoutError, InvalidProxy, InvalidHandshake) as exc:
                pool.mark_failed(route)
                last = exc
                continue
            pool.mark_ok(route)
            self._conn = conn
            return ws
        assert last is not None
        raise last

    async def __aenter__(self) -> Any:
        return await self._open()

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._conn is not None:
            await self._conn.__aexit__(*exc_info)
            self._conn = None

    def __await__(self) -> Any:
        return self._open().__await__()


def ws_connect(url: str, **kwargs: Any) -> _FallbackConnect:
    """Use as the ``connector`` of the Polymarket WebSocket feeds."""

    kwargs.pop("proxy", None)
    return _FallbackConnect(url, **kwargs)


# ------------------------------------------------------------------------------------------------- official SDK
class _ProxiedAsyncClient(httpx.AsyncClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if "transport" not in kwargs and "mounts" not in kwargs:
            kwargs["transport"] = build_transport(limits=kwargs.get("limits"))
        super().__init__(*args, **kwargs)


class _HttpxShim(ModuleType):
    """Looks like ``httpx`` except that ``AsyncClient`` uses the Polymarket proxy transport."""

    AsyncClient = _ProxiedAsyncClient

    def __getattr__(self, name: str) -> Any:
        return getattr(httpx, name)


_sdk_patched = False


def patch_polymarket_sdk() -> None:
    """Make the official ``polymarket`` SDK reach its REST hosts and WebSockets through the proxy list.

    The SDK builds its own httpx clients and WebSocket connections and has no proxy option, so its two
    construction points are replaced. Idempotent."""

    global _sdk_patched
    if _sdk_patched:
        return
    import polymarket._internal.ws.connection as ws_conn
    import polymarket.clients._transport as sdk_transport

    sdk_transport.httpx = _HttpxShim("httpx")  # type: ignore[attr-defined]
    ws_conn.ws_connect = ws_connect  # type: ignore[attr-defined]
    _sdk_patched = True
