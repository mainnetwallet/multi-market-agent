from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from mm_agent.polymarket_data import (
    ENDPOINTS,
    ClobPublic,
    DataAPIError,
    PolymarketData,
)
from mm_agent.venues.polymarket import PolymarketVenue
from tests.conftest import make_settings

USER = "0x983EEDFBD75803602E4A6E6EA9AAB6DC6B9C6748"
COND = "0x" + "ab" * 32
COND2 = "0x" + "cd" * 32


class _Recorder:
    """An httpx client whose transport records requests and answers with ``reply``."""

    def __init__(self, reply: Any = None, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.reply = {"data": []} if reply is None else reply
        self.status = status
        self.headers = headers or {}
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.reply, headers=self.headers)

    def data(self) -> PolymarketData:
        return PolymarketData(lambda: self.client)

    def clob(self) -> ClobPublic:
        return ClobPublic(lambda: self.client)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── Data API v2: request shape ──────────────────────────────────────────────────────────────


def test_positions_sends_lowercased_user_and_status() -> None:
    rec = _Recorder({"data": [{"outcome": "Yes"}], "pagination": {"has_more": False, "next_cursor": None}})
    out = run(rec.data().fetch("positions", {"user": USER, "status": "closed", "limit": "50"}))
    assert out["data"][0]["outcome"] == "Yes"
    req = rec.requests[0]
    assert req.url.path == "/v2/positions" and req.method == "GET"
    assert dict(req.url.params) == {"user": USER.lower(), "status": "CLOSED", "limit": "50"}


def test_cursor_is_passed_back_with_its_anchor() -> None:
    rec = _Recorder()
    run(rec.data().fetch("positions", {"user": USER, "cursor": "eyJkYXRhIjp7InR5cGUiOiJwb3NpdGlvbnMi"}))
    assert dict(rec.requests[0].url.params)["cursor"] == "eyJkYXRhIjp7InR5cGUiOiJwb3NpdGlvbnMi"


def test_conditions_are_deduplicated_and_lowercased() -> None:
    rec = _Recorder()
    run(rec.data().fetch("oi", {"condition": f"{COND.upper().replace('0X', '0x')},{COND},{COND2}"}))
    assert dict(rec.requests[0].url.params)["condition"] == f"{COND},{COND2}"


def test_oi_without_condition_is_the_global_figure() -> None:
    rec = _Recorder({"data": [{"condition_id": "GLOBAL", "value": 1.0}]})
    out = run(rec.data().fetch("oi"))
    assert out["data"][0]["condition_id"] == "GLOBAL" and not rec.requests[0].url.params


def test_empty_values_are_dropped() -> None:
    rec = _Recorder()
    run(rec.data().fetch("trades", {"user": USER, "side": "", "limit": None}))
    assert set(dict(rec.requests[0].url.params)) == {"user"}


def test_activity_type_list_is_checked_against_the_documented_set() -> None:
    rec = _Recorder()
    run(rec.data().fetch("activity", {"user": USER, "type": "trade, redeem,TRADE"}))
    assert dict(rec.requests[0].url.params)["type"] == "TRADE,REDEEM"
    with pytest.raises(ValueError, match="unknown type"):
        run(rec.data().fetch("activity", {"user": USER, "type": "TRADE,DROP"}))


def test_prices_history_forms() -> None:
    rec = _Recorder({"data": []})
    run(rec.data().fetch("prices-history", {"token_id": "123456", "interval": "1W", "bucket_seconds": "300"}))
    assert dict(rec.requests[0].url.params) == {"token_id": "123456", "interval": "1w", "bucket_seconds": "300"}


# ── Data API v2: refusals before any request ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "params", "message"),
    [
        ("positions", {}, "one of user, condition"),
        ("positions", {"user": "0x123"}, "wallet address"),
        ("positions", {"condition": f"{COND},{COND2}"}, "exactly one condition"),
        ("positions", {"user": USER, "status": "ALL"}, "must be one of"),
        ("positions", {"user": USER, "limit": "5000"}, "1..1000"),
        ("positions", {"user": USER, "offset": "10"}, "unknown parameter"),  # v2 has no offset
        ("positions", {"user": USER, "cursor": "a b;c"}, "valid cursor"),
        ("activity", {"condition": COND}, "one of user"),
        ("activity", {"user": USER, "condition": COND, "event_id": "5"}, "mutually exclusive"),
        ("holders", {}, "one of condition"),
        ("holders", {"condition": f"{COND},{COND2}", "include_pnl": "true"}, "exactly one condition"),
        ("holders", {"condition": COND, "include_pnl": "true", "limit": "500"}, "at most 100"),
        ("holders", {"condition": "0xzz"}, "malformed"),
        ("live-volume", {"event_id": "12;DROP"}, "malformed"),
        ("resolutions", {}, "exactly one"),
        ("resolutions", {"condition": COND, "event_id": "9"}, "exactly one"),
        ("tokens", {}, "exactly one"),
        ("prices-history", {"token_id": "1"}, "exactly one of start, interval or as_of"),
        ("prices-history", {"token_id": "1", "interval": "1d", "as_of": "5"}, "exactly one of start"),
        ("prices-history", {"token_id": "1", "end": "9", "interval": "1d"}, "end needs start"),
        ("prices-history", {"token_id": "1", "interval": "1d", "bucket_seconds": "5"}, "60..86400"),
        ("leaderboard", {"time_period": "year"}, "must be one of"),
        ("leaderboard", {"category": "x/../y"}, "may only hold"),
        ("value", {"condition": COND}, "one of user"),
        ("status", {"x": "1"}, "unknown parameter"),
    ],
)
def test_bad_input_is_refused_without_a_request(name: str, params: dict[str, Any], message: str) -> None:
    rec = _Recorder()
    with pytest.raises(ValueError, match=message):
        run(rec.data().fetch(name, params))
    assert rec.requests == []


def test_unknown_route_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown data route"):
        run(_Recorder().data().fetch("../admin"))


def test_every_documented_route_is_present() -> None:
    # the routes of Data API v2 that this build serves (combos and builder boards are not wired)
    assert set(ENDPOINTS) == {
        "positions",
        "value",
        "approvals",
        "user-stats",
        "user-pnl",
        "user-volume",
        "trades",
        "activity",
        "holders",
        "oi",
        "live-volume",
        "resolutions",
        "tokens",
        "prices-history",
        "leaderboard",
        "biggest-winners",
        "status",
    }


# ── Data API v2: venue errors ───────────────────────────────────────────────────────────────


def test_429_keeps_retry_after_and_code() -> None:
    rec = _Recorder(
        {"error": "busy", "code": "rate_limited", "retryable": True, "trace_id": "t"},
        status=429,
        headers={"Retry-After": "7"},
    )
    with pytest.raises(DataAPIError) as err:
        run(rec.data().fetch("trades", {"user": USER}))
    assert err.value.status == 429 and err.value.retry_after == 7.0 and err.value.code == "rate_limited"
    assert err.value.message == "busy"


def test_error_body_that_is_not_json_still_raises_cleanly() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(502, text="<html>bad gateway</html>"))
    )
    with pytest.raises(DataAPIError) as err:
        run(PolymarketData(lambda: client).fetch("status"))
    assert err.value.status == 502 and "bad gateway" in err.value.message


def test_success_body_that_is_not_json_is_a_502() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="nope")))
    with pytest.raises(DataAPIError) as err:
        run(PolymarketData(lambda: client).fetch("status"))
    assert err.value.status == 502


# ── CLOB public prices ──────────────────────────────────────────────────────────────────────


def test_midpoint_spread_last_trade_and_price() -> None:
    rec = _Recorder({"mid_price": "0.45"})
    clob = rec.clob()
    assert run(clob.midpoint("12345")) == {"mid_price": "0.45"}
    run(clob.spread("12345"))
    run(clob.last_trade_price("12345"))
    run(clob.price("12345", "buy"))
    paths = [(r.url.path, dict(r.url.params)) for r in rec.requests]
    assert paths == [
        ("/midpoint", {"token_id": "12345"}),
        ("/spread", {"token_id": "12345"}),
        ("/last-trade-price", {"token_id": "12345"}),
        ("/price", {"token_id": "12345", "side": "BUY"}),
    ]


def test_batch_midpoints_and_last_trades() -> None:
    rec = _Recorder({"1": "0.4", "2": "0.6"})
    clob = rec.clob()
    run(clob.midpoints("1, 2,1"))
    assert dict(rec.requests[0].url.params) == {"token_ids": "1,2"}
    run(clob.last_trades_prices(["1", "2", "2"]))
    req = rec.requests[1]
    assert req.method == "POST" and req.url.path == "/last-trades-prices"
    assert json.loads(req.content) == [{"token_id": "1"}, {"token_id": "2"}]


def test_clob_inputs_are_checked() -> None:
    rec = _Recorder()
    clob = rec.clob()
    with pytest.raises(ValueError):
        run(clob.midpoint("../book"))
    with pytest.raises(ValueError):
        run(clob.price("1", "HOLD"))
    with pytest.raises(ValueError, match="at most 500"):
        run(clob.last_trades_prices([str(i) for i in range(501)]))
    with pytest.raises(ValueError, match="empty"):
        run(clob.last_trades_prices(" , "))
    assert rec.requests == []


# ── wiring: adapter and Control API ─────────────────────────────────────────────────────────


def test_adapter_exposes_the_clients(tmp_path: Path) -> None:
    venue = PolymarketVenue(make_settings(tmp_path))
    assert isinstance(venue.market_data, PolymarketData) and isinstance(venue.clob_public, ClobPublic)
    with pytest.raises(RuntimeError, match="not started"):
        run(venue.market_data.fetch("status"))  # the shared client only exists after start()


class _Stub:
    """Stands in for the Polymarket adapter: only the two data clients, answered by a recorder."""

    def __init__(self, rec: _Recorder) -> None:
        self.market_data = rec.data()
        self.clob_public = rec.clob()


def _api(harness: Any, monkeypatch: pytest.MonkeyPatch, adapter: Any) -> Any:
    from fastapi.testclient import TestClient

    from mm_agent.control_api import create_app
    from mm_agent.domain import VenueId

    # monkeypatch undoes this before the harness closes its adapters
    monkeypatch.setitem(harness.registry.adapters, VenueId.POLYMARKET, adapter)
    app = create_app(harness.settings, harness.agent, harness.scanner, harness.registry, harness.chat, harness.bus)
    client = TestClient(app)
    login = client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
    assert login.status_code == 200, login.text
    return client


def test_api_routes_need_a_session(harness: Any) -> None:
    from fastapi.testclient import TestClient

    from mm_agent.control_api import create_app

    app = create_app(harness.settings, harness.agent, harness.scanner, harness.registry, harness.chat, harness.bus)
    client = TestClient(app)
    assert client.get("/v1/polymarket/data/status").status_code == 401
    assert client.get("/v1/polymarket/price/midpoint?token_id=1").status_code == 401


def test_api_404_when_the_adapter_has_no_data_clients(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _api(harness, monkeypatch, object())
    assert client.get("/v1/polymarket/data/status").status_code == 404
    assert client.get("/v1/polymarket/price/midpoint?token_id=1").status_code == 404


def test_api_data_route_returns_the_venue_envelope(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder({"data": [{"condition_id": COND, "value": 12.5}]})
    client = _api(harness, monkeypatch, _Stub(rec))
    res = client.get(f"/v1/polymarket/data/oi?condition={COND}")
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True, "result": {"data": [{"condition_id": COND, "value": 12.5}]}}
    listing = client.get("/v1/polymarket/data").json()
    assert "positions" in listing["routes"] and "midpoints" in listing["prices"]
    assert "user" in listing["routes"]["positions"]


def test_api_maps_bad_input_to_400_and_venue_429_to_429(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder({"error": "busy", "code": "rate_limited"}, status=429, headers={"Retry-After": "3"})
    client = _api(harness, monkeypatch, _Stub(rec))
    bad = client.get("/v1/polymarket/data/positions?user=nope")
    assert bad.status_code == 400 and "wallet address" in bad.json()["detail"]
    assert client.get("/v1/polymarket/data/nothing").status_code == 400
    busy = client.get(f"/v1/polymarket/data/trades?user={USER}")
    assert busy.status_code == 429 and busy.headers["retry-after"] == "3"
    assert len(rec.requests) == 1  # only the valid request left the process


def test_api_price_routes(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder({"mid_price": "0.45"})
    client = _api(harness, monkeypatch, _Stub(rec))
    assert client.get("/v1/polymarket/price/midpoint?token_id=777").json()["result"] == {"mid_price": "0.45"}
    assert client.get("/v1/polymarket/price/price?token_id=777&side=sell").status_code == 200
    assert client.get("/v1/polymarket/price/midpoints?token_ids=1,2").status_code == 200
    assert client.get("/v1/polymarket/price/midpoint").status_code == 400  # token_id missing
    assert client.get("/v1/polymarket/price/bogus?token_id=1").status_code == 404
    assert [r.url.path for r in rec.requests] == ["/midpoint", "/price", "/midpoints"]


def test_api_unreachable_venue_is_a_502_not_a_crash(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    rec = _Recorder()
    rec.client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    client = _api(harness, monkeypatch, _Stub(rec))
    res = client.get(f"/v1/polymarket/data/trades?user={USER}")
    assert res.status_code == 502 and "unreachable" in res.json()["detail"]
