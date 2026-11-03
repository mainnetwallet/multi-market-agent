from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from mm_agent.polymarket_account import ACCOUNT_ROUTES, PolymarketAccount, plain
from mm_agent.polymarket_data import DataAPIError
from mm_agent.polymarket_explorer import GROUPS, HOST_LIMITS, ROUTES, PolymarketExplorer, clock_skew

USER = "0x983EEDFBD75803602E4A6E6EA9AAB6DC6B9C6748"
COND = "0x" + "ab" * 32
TOKEN = "123456789012345678901234567890"


class _Rec:
    def __init__(
        self, reply: Any = None, *, status: int = 200, text: str | None = None, headers: dict[str, str] | None = None
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.reply, self.status, self.text, self.headers = (
            reply if reply is not None else {},
            status,
            text,
            headers or {},
        )
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.text is not None:
            return httpx.Response(self.status, text=self.text, headers=self.headers)
        return httpx.Response(self.status, json=self.reply, headers=self.headers)

    def explorer(self, clock: Any = None) -> PolymarketExplorer:
        return PolymarketExplorer(lambda: self.client, **({"clock": clock} if clock else {}))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── every registry row is well-formed ───────────────────────────────────────────────────────


def test_every_route_has_known_group_host_and_placeholders() -> None:
    groups = {gid for gid, _ in GROUPS}
    for key, route in ROUTES.items():
        assert route.key == key and route.group in groups
        assert route.host in HOST_LIMITS
        for name in route.path_params:
            assert "{" + name + "}" in route.path, key
        assert route.method in {"GET", "POST"}
        if route.method == "POST":
            assert route.body_list in route.params, key


def test_catalog_lists_every_route_with_class_and_evidence() -> None:
    cat = PolymarketExplorer.catalog()
    assert len(cat["routes"]) == len(ROUTES)
    assert {r["class"] for r in cat["routes"]} == {"R0", "X"}
    assert {r["evidence"] for r in cat["routes"]} <= {"V", "U", "S"}


# ── request shape ───────────────────────────────────────────────────────────────────────────


def test_path_segment_is_validated_and_quoted() -> None:
    rec = _Rec({"id": "1"})
    run(rec.explorer().call("gamma.tag.id", {"id": "42"}))
    assert rec.requests[0].url.path == "/tags/42" and rec.requests[0].url.host == "gamma-api.polymarket.com"
    with pytest.raises(ValueError):
        run(rec.explorer().call("gamma.tag.id", {"id": "../etc"}))
    with pytest.raises(ValueError):
        run(rec.explorer().call("gamma.tag.id", {}))
    assert len(rec.requests) == 1


def test_keyset_markets_sends_after_cursor_and_no_offset() -> None:
    rec = _Rec({"markets": [], "next_cursor": "abc"})
    run(rec.explorer().call("gamma.markets.keyset", {"limit": "5", "after_cursor": "abc", "closed": "false"}))
    assert dict(rec.requests[0].url.params) == {"limit": "5", "after_cursor": "abc", "closed": "false"}
    with pytest.raises(ValueError, match="unknown parameter"):
        run(rec.explorer().call("gamma.markets.keyset", {"offset": "5"}))


def test_post_books_sends_json_array_of_token_ids() -> None:
    rec = _Rec([])
    run(rec.explorer().call("clob.books", {"token_ids": f"{TOKEN},77"}))
    req = rec.requests[0]
    assert req.method == "POST" and req.url.path == "/books"
    assert req.content == b'[{"token_id":"%s"},{"token_id":"77"}]' % TOKEN.encode()


def test_prices_pairs_become_token_and_side_objects() -> None:
    rec = _Rec({})
    run(rec.explorer().call("clob.prices", {"pairs": f"{TOKEN}:buy,{TOKEN}:SELL"}))
    assert rec.requests[0].content == (
        b'[{"token_id":"%s","side":"BUY"},{"token_id":"%s","side":"SELL"}]' % (TOKEN.encode(), TOKEN.encode())
    )
    with pytest.raises(ValueError):
        run(rec.explorer().call("clob.prices", {"pairs": f"{TOKEN}:HOLD"}))


def test_book_limits_and_bad_input_never_reach_the_venue() -> None:
    rec = _Rec([])
    big = ",".join(str(i) for i in range(501))
    with pytest.raises(ValueError, match="at most 500"):
        run(rec.explorer().call("clob.books", {"token_ids": big}))
    with pytest.raises(ValueError):
        run(rec.explorer().call("clob.fee_rate", {"token_id": "not a token"}))
    assert rec.requests == []


def test_comments_need_a_parent_and_wallet_comments_lowercase_the_address() -> None:
    rec = _Rec([])
    with pytest.raises(ValueError, match="one of parent_entity_id"):
        run(rec.explorer().call("gamma.comments", {"limit": "5"}))
    run(rec.explorer().call("gamma.comments.user", {"address": USER}))
    assert rec.requests[0].url.path == "/comments/user_address/" + USER.lower()


def test_data_extras_route_to_the_data_host() -> None:
    rec = _Rec({"data": []})
    run(rec.explorer().call("data.builders.leaderboard", {"time_period": "WEEK"}))
    run(rec.explorer().call("data.positions.combos", {"user": USER}))
    assert [r.url.path for r in rec.requests] == ["/v2/builders/leaderboard", "/v2/positions/combos"]
    assert dict(rec.requests[0].url.params) == {"time_period": "week"}
    with pytest.raises(ValueError, match="required"):
        run(rec.explorer().call("data.positions.combos", {}))


# ── response kinds ──────────────────────────────────────────────────────────────────────────


def test_text_route_returns_plain_text_and_json_route_fails_cleanly_on_html() -> None:
    assert run(_Rec(text="1767225600\n").explorer().call("clob.time")) == "1767225600"
    with pytest.raises(DataAPIError) as err:
        run(_Rec(text="<html>").explorer().call("gamma.sports"))
    assert err.value.status == 502


def test_bytes_route_returns_the_zip_untouched() -> None:
    rec = _Rec(text="PK\x03\x04zipbytes")
    out = run(rec.explorer().call("data.accounting", {"user": USER}))
    assert isinstance(out, bytes) and out.startswith(b"PK")
    assert rec.requests[0].url.path == "/v1/accounting/snapshot"


# ── errors, limits, cache ───────────────────────────────────────────────────────────────────


def test_429_keeps_retry_after_and_is_not_retried() -> None:
    rec = _Rec({"error": "slow down"}, status=429, headers={"Retry-After": "7"})
    with pytest.raises(DataAPIError) as err:
        run(rec.explorer().call("gamma.sports"))
    assert err.value.status == 429 and err.value.retry_after == 7.0
    assert len(rec.requests) == 1


def test_unverified_path_404_is_reported_as_not_available() -> None:
    rec = _Rec({"error": "nope"}, status=404)
    with pytest.raises(DataAPIError) as err:
        run(rec.explorer().call("clob.markets"))
    assert "not available" in err.value.message
    with pytest.raises(DataAPIError) as err2:
        run(rec.explorer().call("gamma.sports"))
    assert err2.value.message == "nope"


def test_cache_serves_the_second_read_and_expires() -> None:
    now = [100.0]
    rec = _Rec([{"id": 1}])
    ex = rec.explorer(clock=lambda: now[0])
    run(ex.call("gamma.sports"))
    run(ex.call("gamma.sports"))
    assert len(rec.requests) == 1
    now[0] += 4000
    run(ex.call("gamma.sports"))
    assert len(rec.requests) == 2


def test_local_rate_guard_stops_before_the_venue_does() -> None:
    now = [0.0]
    rec = _Rec({"status": "ok"})
    ex = rec.explorer(clock=lambda: now[0])
    for _ in range(100):
        run(ex.call("clob.health"))
    with pytest.raises(DataAPIError) as err:
        run(ex.call("clob.health"))
    assert err.value.status == 429 and err.value.retry_after
    now[0] += 11
    run(ex.call("clob.health"))


def test_unknown_route_key() -> None:
    with pytest.raises(KeyError):
        run(_Rec().explorer().call("nope"))


def test_clock_skew_verdict() -> None:
    assert clock_skew("1000", 1002.0)["ok"] is True
    assert clock_skew("1000", 1060.0)["ok"] is False
    assert clock_skew("abc", 1.0)["ok"] is False


# ── account (R1): no credential ever leaves ─────────────────────────────────────────────────


class _Page:
    def __init__(self, items: list[Any]) -> None:
        self.items, self.limit_reached, self.has_more = items, False, False


class _Pager:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            yield _Page(self._items)

        return gen()


class _FakeSecure:
    async def fetch_api_keys(self) -> tuple[str, ...]:
        return ("11111111-2222-3333-4444-555555555555",)

    def list_user_earnings_for_day(self, *, date: str) -> Any:
        return _Pager([{"market": "m", "earnings": "1.5", "secret": "S3CRET", "passphrase": "PW"}])

    async def get_order_scoring(self, *, order_id: str) -> bool:
        return True

    async def get_closed_only_mode(self) -> bool:
        return False


def _account() -> PolymarketAccount:
    async def get() -> Any:
        return _FakeSecure()

    return PolymarketAccount(get)


def test_api_keys_are_masked_and_secret_fields_are_dropped() -> None:
    keys = run(_account().call("api_keys"))
    assert "11111111-2222-3333-4444-555555555555" not in str(keys) and keys["count"] == 1
    day = run(_account().call("rewards.day", {"date": "2026-11-01"}))
    text = str(day)
    assert "S3CRET" not in text and "PW" not in text and "1.5" in text
    assert plain({"a": {"api_key": "k", "x": 1}}) == {"a": {"x": 1}}


def test_account_routes_validate_and_work() -> None:
    assert run(_account().call("scoring", {"order_ids": "0xabc"})) == {"0xabc": True}
    assert run(_account().call("closed_only")) == {"closed_only": False}
    with pytest.raises(ValueError):
        run(_account().call("rewards.day", {"date": "yesterday"}))
    with pytest.raises(ValueError, match="unknown parameter"):
        run(_account().call("closed_only", {"x": "1"}))
    with pytest.raises(KeyError):
        run(_account().call("create_api_key"))
    assert all(r.key and not r.key.startswith(("post", "delete", "create")) for r in ACCOUNT_ROUTES.values())


# ── Control API: catalog, explore, account, system ─────────────────────────────────────────

from tests.test_polymarket_data import _api, _Recorder  # noqa: E402


class _Stub:
    def __init__(self, rec: _Rec, secure: Any = None) -> None:
        data = _Recorder({"data": [{"user": USER}]})
        data.client = rec.client
        self.market_data = data.data()
        self.explorer = rec.explorer()

        async def get() -> Any:
            if secure is None:
                from mm_agent.venues.base import VenueAuthError

                raise VenueAuthError("Polymarket signer key absent; authenticated features unavailable")
            return secure

        self.account = PolymarketAccount(get)


def test_new_routes_need_a_session(harness: Any) -> None:
    from fastapi.testclient import TestClient

    from mm_agent.control_api import create_app

    app = create_app(harness.settings, harness.agent, harness.scanner, harness.registry, harness.chat, harness.bus)
    client = TestClient(app)
    for path in ("catalog", "explore/gamma.sports", "account/closed_only", "system"):
        assert client.get("/v1/polymarket/" + path).status_code == 401, path


def test_catalog_route_merges_registry_account_and_data_rows(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _api(harness, monkeypatch, _Stub(_Rec()))
    body = client.get("/v1/polymarket/catalog").json()
    keys = {r["key"] for r in body["routes"]}
    assert {"gamma.sports", "clob.books", "account.rewards.day", "data.positions", "data.leaderboard"} <= keys
    assert {r["class"] for r in body["routes"]} == {"R0", "R1", "X"}
    assert body["groups"][0]["id"] == "wallet"


def test_explore_runs_registry_data_and_unknown_rows(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Rec([{"id": 1}])
    client = _api(harness, monkeypatch, _Stub(rec))
    assert client.get("/v1/polymarket/explore/gamma.sports").json() == {"ok": True, "result": [{"id": 1}]}
    assert client.get(f"/v1/polymarket/explore/data.positions?user={USER}").json()["ok"] is True
    assert client.get("/v1/polymarket/explore/gamma.tag.id?id=x").status_code == 400
    assert client.get("/v1/polymarket/explore/nothing").status_code == 404


def test_explore_streams_the_accounting_zip(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _api(harness, monkeypatch, _Stub(_Rec(text="PK\x03\x04data")))
    res = client.get(f"/v1/polymarket/explore/data.accounting?user={USER}")
    assert res.status_code == 200 and res.headers["content-type"] == "application/zip"
    assert res.content.startswith(b"PK") and "attachment" in res.headers["content-disposition"]


def test_account_route_says_needs_credentials_instead_of_failing(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _api(harness, monkeypatch, _Stub(_Rec()))
    res = client.get("/v1/polymarket/account/closed_only")
    assert res.status_code == 200 and res.json()["needs_credentials"] is True and res.json()["ok"] is False
    assert client.get("/v1/polymarket/account/create_api_key").status_code == 404


def test_account_route_hides_secrets_end_to_end(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _api(harness, monkeypatch, _Stub(_Rec(), secure=_FakeSecure()))
    res = client.get("/v1/polymarket/account/rewards.day?date=2026-11-01")
    assert res.status_code == 200 and "S3CRET" not in res.text and "PW" not in res.text
    keys = client.get("/v1/polymarket/account/api_keys")
    assert "11111111-2222" not in keys.text


def test_system_route_reports_each_check_on_its_own(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    rec = _Rec(text=str(int(time.time())))
    client = _api(harness, monkeypatch, _Stub(rec))
    checks = client.get("/v1/polymarket/system").json()["checks"]
    assert {"gamma", "clob", "data", "geoblock", "time", "v2_status", "clock"} <= set(checks)
    assert checks["clock"]["ok"] is True


def test_adapter_has_the_new_clients_and_does_not_advertise_batch_orders(tmp_path: Any) -> None:
    from mm_agent.venues.polymarket import PolymarketVenue
    from tests.conftest import make_settings

    venue = PolymarketVenue(make_settings(tmp_path))
    assert isinstance(venue.explorer, PolymarketExplorer) and isinstance(venue.account, PolymarketAccount)
    assert venue.caps.batch_orders is False


# ── perps public reads, combos and relayer reads ───────────────────────────────────────────


def test_perps_combos_and_relayer_rows_hit_their_own_hosts() -> None:
    rec = _Rec([{"id": 1}])
    ex = rec.explorer()
    run(ex.call("perps.tickers", {"instrument_id": "3"}))
    run(ex.call("perps.book", {"instrument_id": "3", "depth": "100"}))
    run(ex.call("perps.klines", {"instrument_id": "3", "interval": "1H", "start_timestamp": "1700000000000"}))
    run(ex.call("combos.markets", {"exclude": "a,b"}))
    run(ex.call("relayer.relay_payload", {"address": USER, "type": "safe"}))
    run(ex.call("relayer.tx", {"transaction_id": "abc-1"}))
    urls = [str(r.url) for r in rec.requests]
    assert urls[0] == "https://api.perpetuals.polymarket.com/v1/info/tickers?instrument_id=3"
    assert urls[1].endswith("/v1/info/book?instrument_id=3&depth=100")
    assert "interval=1h" in urls[2] and "start_timestamp=1700000000000" in urls[2]
    assert urls[3].startswith("https://combos-rfq-api.polymarket.com/v1/rfq/combo-markets?exclude=a%2Cb")
    assert urls[4].startswith("https://relayer-v2.polymarket.com/relay-payload?") and "type=SAFE" in urls[4]
    assert urls[5] == "https://relayer-v2.polymarket.com/v1/account/transactions/abc-1"


def test_perps_rows_are_class_x_and_validate_before_sending() -> None:
    rec = _Rec()
    ex = rec.explorer()
    assert {r["class"] for r in ex.catalog()["routes"] if r["host"] == "perps"} == {"X"}
    for key, bad in (
        ("perps.book", {"instrument_id": "3", "depth": "7"}),
        ("perps.book", {}),
        ("perps.klines", {"instrument_id": "3", "interval": "2h"}),
        ("perps.tickers", {"instrument_id": "-1"}),
        ("perps.registered", {"address": "nope"}),
        ("combos.markets", {"nope": "1"}),
    ):
        with pytest.raises(ValueError):
            run(ex.call(key, bad))
    assert rec.requests == []


def test_unverified_sdk_rows_report_not_available_on_404() -> None:
    rec = _Rec({"error": "not found"}, status=404)
    ex = rec.explorer()
    with pytest.raises(DataAPIError) as err:
        run(ex.call("perps.tickers", {}))
    assert err.value.status == 404 and "not available" in str(err.value)
