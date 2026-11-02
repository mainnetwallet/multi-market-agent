"""Polymarket-only proxy routing with ordered fallback (no network)."""

from __future__ import annotations

import httpx
import pytest

from mm_agent import proxy


class _Fake(httpx.AsyncBaseTransport):
    def __init__(self, name: str, log: list[str], *, fail: type[Exception] | None = None, status: int = 200) -> None:
        self.name, self.log, self.fail, self.status = name, log, fail, status

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.log.append(self.name)
        if self.fail is not None:
            raise self.fail("boom", request=request)
        return httpx.Response(self.status, json={"via": self.name}, request=request)


def _transport(
    urls: list[str],
    fails: dict[str, type[Exception]] | None = None,
    blocked: dict[str, int] | None = None,
    **kw: object,
) -> tuple[proxy.PolymarketProxyTransport, list[str]]:
    log: list[str] = []
    pool = proxy.ProxyPool(urls, **kw)  # type: ignore[arg-type]
    t = proxy.PolymarketProxyTransport(pool)
    t._direct = _Fake("direct", log, fail=(fails or {}).get("direct"), status=(blocked or {}).get("direct", 200))  # type: ignore[assignment]
    t._proxied = {u: _Fake(u, log, fail=(fails or {}).get(u), status=(blocked or {}).get(u, 200)) for u in urls}  # type: ignore[misc]
    return t, log


async def _call(t: proxy.PolymarketProxyTransport, url: str, method: str = "GET") -> httpx.Response:
    async with httpx.AsyncClient(transport=t) as c:
        return await c.request(method, url)


def test_parse_proxy_list() -> None:
    assert proxy.parse_proxy_list("1.1.1.1:80, http://u:p@h:8080\nhttp://h:8080;1.1.1.1:80") == [
        "http://1.1.1.1:80",
        "http://u:p@h:8080",
        "http://h:8080",
    ]
    assert proxy.parse_proxy_list("") == []


def test_redact_hides_credentials() -> None:
    assert proxy.redact("http://user:secret@10.0.0.1:3128") == "http://10.0.0.1:3128"


def test_three_public_proxies_by_default() -> None:
    assert len(proxy.PUBLIC_PROXIES) == 3


async def test_only_polymarket_hosts_use_the_proxy() -> None:
    t, log = _transport(["http://p1"])
    await _call(t, "https://clob.polymarket.com/book")
    await _call(t, "https://api.elections.kalshi.com/x")
    await _call(t, "https://notpolymarket.com/x")
    assert log == ["http://p1", "direct", "direct"]


async def test_falls_back_to_next_proxy_in_order() -> None:
    t, log = _transport(["http://p1", "http://p2", "http://p3"], {"http://p1": httpx.ConnectError})
    r = await _call(t, "https://gamma-api.polymarket.com/markets")
    assert r.json() == {"via": "http://p2"}
    assert log == ["http://p1", "http://p2"]
    log.clear()
    await _call(t, "https://gamma-api.polymarket.com/markets")  # p1 is cooling down: p2 first now
    assert log == ["http://p2"]


async def test_all_proxies_fail_raises_and_never_goes_direct() -> None:
    t, log = _transport(["http://p1", "http://p2"], {"http://p1": httpx.ProxyError, "http://p2": httpx.ConnectTimeout})
    with pytest.raises(httpx.TransportError):
        await _call(t, "https://clob.polymarket.com/book")
    assert "direct" not in log


async def test_direct_fallback_is_opt_in() -> None:
    t, log = _transport(["http://p1"], {"http://p1": httpx.ConnectError}, direct_fallback=True)
    await _call(t, "https://clob.polymarket.com/book")
    assert log == ["http://p1", "direct"]


async def test_post_is_not_replayed_after_a_read_timeout() -> None:
    t, log = _transport(["http://p1", "http://p2"], {"http://p1": httpx.ReadTimeout})
    with pytest.raises(httpx.ReadTimeout):
        await _call(t, "https://clob.polymarket.com/order", "POST")
    assert log == ["http://p1"]  # the order may have reached Polymarket: never sent twice


async def test_get_retries_after_a_read_timeout() -> None:
    t, _log = _transport(["http://p1", "http://p2"], {"http://p1": httpx.ReadTimeout})
    r = await _call(t, "https://clob.polymarket.com/book")
    assert r.json() == {"via": "http://p2"}


async def test_post_moves_on_when_the_tunnel_could_not_open() -> None:
    t, _log = _transport(["http://p1", "http://p2"], {"http://p1": httpx.ProxyError})
    r = await _call(t, "https://clob.polymarket.com/order", "POST")
    assert r.json() == {"via": "http://p2"}


async def test_disabled_pool_goes_direct() -> None:
    t, log = _transport(["http://p1"], enabled=False)
    await _call(t, "https://clob.polymarket.com/book")
    assert log == ["direct"]


async def test_direct_first_uses_the_server_ip_when_it_works() -> None:
    t, log = _transport(["http://p1", "http://p2"], direct_first=True)
    r = await _call(t, "https://clob.polymarket.com/book")
    assert r.json() == {"via": "direct"}
    assert log == ["direct"]


async def test_direct_first_then_public_proxies_in_order() -> None:
    t, log = _transport(
        ["http://p1", "http://p2"], {"direct": httpx.ConnectError, "http://p1": httpx.ProxyError}, direct_first=True
    )
    r = await _call(t, "https://clob.polymarket.com/book")
    assert r.json() == {"via": "http://p2"}
    assert log == ["direct", "http://p1", "http://p2"]
    log.clear()
    await _call(t, "https://clob.polymarket.com/book")  # direct and p1 are cooling down: p2 first now
    assert log == ["http://p2"]


async def test_geo_block_on_direct_moves_on_even_for_post() -> None:
    t, log = _transport(["http://p1"], blocked={"direct": 403}, direct_first=True)
    r = await _call(t, "https://clob.polymarket.com/order", "POST")
    assert r.json() == {"via": "http://p1"}
    assert log == ["direct", "http://p1"]


async def test_every_route_blocked_returns_the_real_403() -> None:
    t, log = _transport(["http://p1"], blocked={"direct": 403, "http://p1": 403}, direct_first=True)
    r = await _call(t, "https://clob.polymarket.com/book")
    assert r.status_code == 403
    assert log == ["direct", "http://p1"]


async def test_every_route_fails_raises_the_last_error() -> None:
    t, _ = _transport(["http://p1"], {"direct": httpx.ConnectError, "http://p1": httpx.ProxyError}, direct_first=True)
    with pytest.raises(httpx.ProxyError):
        await _call(t, "https://clob.polymarket.com/book")


async def test_non_polymarket_hosts_stay_direct_with_direct_first() -> None:
    t, log = _transport(["http://p1"], {"direct": httpx.ConnectError}, direct_first=True)
    with pytest.raises(httpx.ConnectError):
        await _call(t, "https://api.elections.kalshi.com/x")
    assert log == ["direct"]
