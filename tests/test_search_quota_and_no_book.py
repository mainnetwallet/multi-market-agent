from __future__ import annotations

import asyncio

import httpx

from mm_agent import llm
from mm_agent.books import BookCache, is_no_book_error


class _Resp:
    def __init__(self, status: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


def _client_factory(responses: list[_Resp], calls: list[int]):
    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

        async def post(self, *a, **k):
            calls.append(1)
            return responses.pop(0)

    return _Client


def test_search_pauses_after_quota_rejection(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    calls: list[int] = []
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory([_Resp(432, text="limit")], calls))
    client = llm.WebSearchClient()
    assert client.enabled
    assert asyncio.run(client.search("q")) == []
    assert not client.enabled and client.configured
    assert client.blocked_status == 432 and client.blocked_seconds > 0
    # paused: no further provider calls
    assert asyncio.run(client.search("q")) == []
    assert len(calls) == 1


def test_search_recovers_after_pause(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    calls: list[int] = []
    ok = _Resp(200, {"results": [{"title": "t", "url": "https://x.test", "content": "c"}]})
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory([_Resp(429), ok], calls))
    client = llm.WebSearchClient()
    asyncio.run(client.search("q"))
    assert not client.enabled
    client._blocked_until = 0.0  # pause elapsed
    out = asyncio.run(client.search("q"))
    assert out and out[0]["url"] == "https://x.test"
    assert client.blocked_status is None


def test_no_book_errors_are_remembered():
    err = RuntimeError('venue HTTP 404 at https://clob.polymarket.com/book: {"error":"No orderbook exists"}')
    assert is_no_book_error(err)
    assert not is_no_book_error(RuntimeError("venue HTTP 500"))
    cache = BookCache()
    assert not cache.has_no_book("polymarket", "1")
    cache.mark_no_book("polymarket", "1")
    assert cache.has_no_book("polymarket", "1")
    assert not cache.has_no_book("polymarket", "2")
    cache._no_book["polymarket:1"] = 0.0  # expired
    assert not cache.has_no_book("polymarket", "1")
