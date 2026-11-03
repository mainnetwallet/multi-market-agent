from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mm_agent import polymarket_write as pw
from mm_agent.config import Settings
from mm_agent.polymarket_write import ACTIONS, HEARTBEAT_ACK, HeartbeatLoop, PolymarketWriter, WriteError

ADDR = "0x983EEDFBD75803602E4A6E6EA9AAB6DC6B9C6748"
ORDER = "0x" + "ab" * 32
ON = {
    "polymarket_write_api": True,
    "polymarket_write_orders": True,
    "polymarket_write_keys": True,
    "polymarket_write_combos_rfq": True,
    "polymarket_heartbeat_enabled": True,
}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Settings:
    def __init__(self, **flags: bool) -> None:
        for name in ("polymarket_write_api", *pw.FAMILY_FLAGS.values()):
            setattr(self, name, flags.get(name, False))


class _FakeSecure:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail
        self.beat = 0

    async def _record(self, name: str, **kw: Any) -> Any:
        self.calls.append((name, kw))
        if self.fail is not None:
            raise self.fail
        return {"ok": True, "secret": "S3CRET", "passphrase": "PW", "api_key": "KEYKEYKEYKEY"}

    async def cancel_orders(self, **kw: Any) -> Any:
        return await self._record("cancel_orders", **kw)

    async def drop_notifications(self, **kw: Any) -> Any:
        return await self._record("drop_notifications", **kw)

    async def delete_api_key(self, **kw: Any) -> Any:
        return await self._record("delete_api_key", **kw)

    async def revoke_builder_api_key(self, **kw: Any) -> Any:
        return await self._record("revoke_builder_api_key", **kw)

    async def authorize_session_key(self, **kw: Any) -> Any:
        return await self._record("authorize_session_key", **kw)

    async def revoke_session_key(self, **kw: Any) -> Any:
        return await self._record("revoke_session_key", **kw)

    async def request_combo_quote(self, **kw: Any) -> Any:
        return await self._record("request_combo_quote", **kw)

    async def plan_collateral_return(self, **kw: Any) -> Any:
        return await self._record("plan_collateral_return", **kw)

    async def send_order_heartbeat(self, **kw: Any) -> Any:
        self.beat += 1
        await self._record("send_order_heartbeat", **kw)

        class Beat:
            heartbeat_id = f"hb{self.beat}"

        return Beat()


def _writer(secure: _FakeSecure, place: Any = None) -> PolymarketWriter:
    async def get() -> Any:
        return secure

    return PolymarketWriter(get, place_order=place)


# ── registry ───────────────────────────────────────────────────────────────────────────────


def test_every_flag_exists_and_defaults_to_false() -> None:
    fields = Settings.model_fields
    for name in ("polymarket_write_api", *pw.FAMILY_FLAGS.values()):
        assert name in fields and fields[name].default is False, name


def test_catalog_reports_everything_disabled_by_default_and_lists_what_is_not_built() -> None:
    cat = pw.catalog(_Settings())
    assert cat["enabled"] is False
    assert {a["key"] for a in cat["actions"]} == set(ACTIONS)
    assert all(a["enabled"] is False and a["class"] == "W" for a in cat["actions"])
    assert any("POLYMARKET_WRITE_API" in a["reason"] for a in cat["actions"])
    assert cat["not_built"]


def test_both_the_global_and_the_family_flag_are_needed() -> None:
    assert pw.gate_state(_Settings(polymarket_write_orders=True), "orders")[0] is False
    assert pw.gate_state(_Settings(polymarket_write_api=True), "orders")[0] is False
    assert pw.gate_state(_Settings(polymarket_write_api=True, polymarket_write_orders=True), "orders") == (True, "")


# ── actions ────────────────────────────────────────────────────────────────────────────────


def test_disabled_actions_never_reach_the_venue() -> None:
    secure = _FakeSecure()
    with pytest.raises(WriteError) as err:
        run(_writer(secure).execute("orders.cancel_ids", {"order_ids": ORDER}, settings=_Settings(), actor="t"))
    assert err.value.code == "write_disabled" and secure.calls == []


def test_cancel_by_ids_validates_and_hides_secrets() -> None:
    secure = _FakeSecure()
    out = run(
        _writer(secure).execute(
            "orders.cancel_ids", {"order_ids": f"{ORDER},{ORDER}"}, settings=_Settings(**ON), actor="t"
        )
    )
    assert secure.calls == [("cancel_orders", {"order_ids": [ORDER]})]
    assert out["outcome"] == "OK" and "S3CRET" not in str(out) and "PW" not in str(out)
    with pytest.raises(ValueError):
        run(_writer(secure).execute("orders.cancel_ids", {"order_ids": "bad id"}, settings=_Settings(**ON), actor="t"))
    with pytest.raises(ValueError):
        run(
            _writer(secure).execute(
                "orders.cancel_ids", {"order_ids": ORDER, "x": 1}, settings=_Settings(**ON), actor="t"
            )
        )
    with pytest.raises(ValueError):
        run(_writer(secure).execute("orders.cancel_ids", {}, settings=_Settings(**ON), actor="t"))


@pytest.mark.parametrize(
    ("key", "ack"),
    [("keys.api.delete", "DELETE API KEY"), ("keys.builder.revoke", "REVOKE BUILDER KEY")],
)
def test_irreversible_actions_need_the_typed_text(key: str, ack: str) -> None:
    secure = _FakeSecure()
    for bad in ({}, {"confirm": "yes"}, {"confirm": ack.lower()}):
        with pytest.raises(WriteError) as err:
            run(_writer(secure).execute(key, bad, settings=_Settings(**ON), actor="t"))
        assert err.value.code == "ack_required"
    assert secure.calls == []
    assert run(_writer(secure).execute(key, {"confirm": ack}, settings=_Settings(**ON), actor="t"))["outcome"] == "OK"
    assert len(secure.calls) == 1


def test_session_signer_scopes_and_address_are_checked() -> None:
    secure = _FakeSecure()
    body = {"address": ADDR, "scopes": "clob", "confirm": "AUTHORIZE SESSION SIGNER"}
    run(_writer(secure).execute("session.authorize", body, settings=_Settings(**ON), actor="t"))
    assert secure.calls[0][1] == {"address": ADDR.lower(), "scopes": ["CLOB"]}
    for bad in ({"address": "nope"}, {"address": ADDR, "scopes": "ROOT"}):
        with pytest.raises(ValueError):
            run(
                _writer(secure).execute(
                    "session.authorize",
                    {**bad, "confirm": "AUTHORIZE SESSION SIGNER"},
                    settings=_Settings(**ON),
                    actor="t",
                )
            )
    run(_writer(secure).execute("session.revoke", {"address": ADDR}, settings=_Settings(**ON), actor="t"))
    assert secure.calls[-1][0] == "revoke_session_key"


def test_combo_quote_needs_exactly_one_of_amount_and_size() -> None:
    secure = _FakeSecure()
    base = {"leg_position_ids": "123,456", "direction": "buy"}
    with pytest.raises(ValueError):
        run(_writer(secure).execute("combos.request_quote", base, settings=_Settings(**ON), actor="t"))
    with pytest.raises(ValueError):
        run(
            _writer(secure).execute(
                "combos.request_quote", {**base, "amount": "5", "size": "5"}, settings=_Settings(**ON), actor="t"
            )
        )
    run(_writer(secure).execute("combos.request_quote", {**base, "amount": "5"}, settings=_Settings(**ON), actor="t"))
    assert secure.calls[0][1]["leg_position_ids"] == ["123", "456"] and secure.calls[0][1]["direction"] == "BUY"


def test_a_failure_after_the_request_is_unknown_and_a_4xx_is_rejected() -> None:
    class Boom(Exception):
        pass

    unknown = run(
        _writer(_FakeSecure(fail=Boom("x"))).execute(
            "notifications.drop", {"ids": "1,2"}, settings=_Settings(**ON), actor="t"
        )
    )
    assert unknown == {"outcome": "UNKNOWN", "detail": "Boom"}

    class Rejected(Exception):
        status = 400

    rejected = run(
        _writer(_FakeSecure(fail=Rejected("nope"))).execute(
            "notifications.drop", {"ids": "1"}, settings=_Settings(**ON), actor="t"
        )
    )
    assert rejected["outcome"] == "REJECTED" and rejected["status"] == 400


# ── guarded batch ──────────────────────────────────────────────────────────────────────────


def _order(n: int = 1) -> dict[str, Any]:
    return {"market_id": f"m{n}", "outcome": "YES", "side": "BUY", "price": "0.5", "shares": "10"}


def test_batch_places_each_order_through_the_guarded_path_and_stops_at_the_first_failure() -> None:
    seen: list[str] = []

    async def place(**kw: Any) -> dict[str, Any]:
        seen.append(kw["market_id"])
        if kw["market_id"] == "m2":
            raise RuntimeError("per-trade cap exceeded")
        return {"order_id": kw["market_id"]}

    out = run(
        _writer(_FakeSecure(), place).execute(
            "orders.batch", {"orders": [_order(1), _order(2), _order(3)]}, settings=_Settings(**ON), actor="t"
        )
    )
    assert seen == ["m1", "m2"]  # m3 is never attempted
    assert out["outcome"] == "PARTIAL" and out["placed"] == 1 and out["results"][1]["ok"] is False


def test_batch_limits_and_shape() -> None:
    async def place(**kw: Any) -> dict[str, Any]:
        return {}

    w = _writer(_FakeSecure(), place)
    for bad in ([], [_order()] * 16, "x", [{"market_id": "m"}], [{**_order(), "extra": 1}]):
        with pytest.raises(ValueError):
            run(w.execute("orders.batch", {"orders": bad}, settings=_Settings(**ON), actor="t"))
    with pytest.raises(WriteError):
        run(_writer(_FakeSecure()).execute("orders.batch", {"orders": [_order()]}, settings=_Settings(**ON), actor="t"))


def test_batch_post_only_string_false_is_not_post_only_and_bad_fields_fail_before_any_order() -> None:
    seen: list[dict[str, Any]] = []

    async def place(**kw: Any) -> dict[str, Any]:
        seen.append(kw)
        return {"order_id": kw["market_id"]}

    w = _writer(_FakeSecure(), place)
    out = run(
        w.execute(
            "orders.batch",
            {"orders": [{**_order(1), "post_only": "false", "outcome": "no"}, {**_order(2), "post_only": True}]},
            settings=_Settings(**ON),
            actor="t",
        )
    )
    assert out["outcome"] == "OK"
    assert seen[0]["post_only"] is False and seen[0]["outcome"] == "NO"
    assert seen[1]["post_only"] is True

    seen.clear()
    for bad in (
        {**_order(2), "post_only": "maybe"},
        {**_order(2), "price": "NaN"},
        {**_order(2), "shares": "0"},
        {**_order(2), "side": "HOLD"},
        {**_order(2), "outcome": "MAYBE"},
    ):
        with pytest.raises(ValueError):
            run(w.execute("orders.batch", {"orders": [_order(1), bad]}, settings=_Settings(**ON), actor="t"))
    assert seen == []  # the good first order was not sent either: the batch is checked as a whole first


# ── heartbeat ──────────────────────────────────────────────────────────────────────────────


def test_heartbeat_does_not_start_without_flags_or_the_typed_text() -> None:
    async def go() -> None:
        beat = HeartbeatLoop(lambda: _get(_FakeSecure()), interval=0)
        with pytest.raises(WriteError):
            await beat.start(_Settings(), HEARTBEAT_ACK)
        with pytest.raises(WriteError):
            await beat.start(_Settings(polymarket_write_api=True), HEARTBEAT_ACK)
        with pytest.raises(WriteError) as err:
            await beat.start(_Settings(**ON), "ok")
        assert err.value.code == "ack_required" and not beat.running

    run(go())


async def _get(secure: Any) -> Any:
    return secure


def test_heartbeat_loop_echoes_the_id_and_stops_on_request() -> None:
    async def go() -> None:
        secure = _FakeSecure()
        beat = HeartbeatLoop(lambda: _get(secure), interval=0.01)
        state = await beat.start(_Settings(**ON), HEARTBEAT_ACK)
        assert state["running"] is True
        await asyncio.sleep(0.12)
        await beat.stop()
        assert not beat.running and beat.sent >= 2
        ids = [kw["heartbeat_id"] for _, kw in secure.calls]
        assert ids[0] == "" and ids[1] == "hb1" and ids[2] == "hb2"
        sent = len(secure.calls)
        await asyncio.sleep(0.05)
        assert len(secure.calls) == sent  # nothing is sent after stop

    run(go())


def test_heartbeat_single_send_needs_the_typed_text_too() -> None:
    secure = _FakeSecure()
    with pytest.raises(WriteError):
        run(_writer(secure).execute("heartbeat.send", {}, settings=_Settings(**ON), actor="t"))
    assert secure.calls == []


# ── HTTP routes ────────────────────────────────────────────────────────────────────────────


class _Adapter:
    def __init__(self, secure: _FakeSecure) -> None:
        self.secure = secure

    async def _secure(self) -> Any:
        return self.secure


def _client(harness: Any, monkeypatch: pytest.MonkeyPatch, secure: _FakeSecure, **flags: bool) -> Any:
    from fastapi.testclient import TestClient

    from mm_agent.control_api import create_app
    from mm_agent.domain import VenueId

    monkeypatch.setitem(harness.registry.adapters, VenueId.POLYMARKET, _Adapter(secure))
    settings = harness.settings.model_copy(update=flags)
    app = create_app(settings, harness.agent, harness.scanner, harness.registry, harness.chat, harness.bus)
    client = TestClient(app)
    assert client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"}).status_code == 200
    return client


def _stepup(client: Any) -> dict[str, str]:
    res = client.post("/v1/auth/stepup", json={"password": "test-password", "action": "polymarket_write"})
    assert res.status_code == 200, res.text
    return {"X-Stepup-Token": res.json()["stepup_token"]}


def test_routes_need_a_session(harness: Any) -> None:
    from fastapi.testclient import TestClient

    from mm_agent.control_api import create_app

    client = TestClient(
        create_app(harness.settings, harness.agent, harness.scanner, harness.registry, harness.chat, harness.bus)
    )
    assert client.get("/v1/polymarket/write").status_code == 401
    assert client.post("/v1/polymarket/write/orders.cancel_ids", json={}).status_code == 401
    assert client.post("/v1/polymarket/heartbeat/start", json={}).status_code == 401
    assert client.post("/v1/polymarket/heartbeat/stop", json={}).status_code == 401


def test_route_is_disabled_by_default_and_spends_nothing(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    secure = _FakeSecure()
    client = _client(harness, monkeypatch, secure)
    cat = client.get("/v1/polymarket/write").json()
    assert cat["enabled"] is False and cat["heartbeat"]["running"] is False
    res = client.post(
        "/v1/polymarket/write/orders.cancel_ids", json={"order_ids": ORDER}, headers={"Idempotency-Key": "k1"}
    )
    assert res.status_code == 403 and res.json()["error"]["code"] == "write_disabled"
    assert secure.calls == []
    assert client.post("/v1/polymarket/write/nothing", json={}).status_code == 404


def test_enabled_route_needs_idempotency_key_and_stepup(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    secure = _FakeSecure()
    client = _client(harness, monkeypatch, secure, **ON)
    url = "/v1/polymarket/write/orders.cancel_ids"
    body = {"order_ids": ORDER}
    no_key = client.post(url, json=body)
    assert no_key.status_code == 400 and no_key.json()["error"]["code"] == "idempotency_required"
    no_step = client.post(url, json=body, headers={"Idempotency-Key": "k2"})
    assert no_step.status_code == 403 and no_step.json()["error"]["code"] == "stepup_required"
    assert secure.calls == []
    ok = client.post(url, json=body, headers={"Idempotency-Key": "k3", **_stepup(client)})
    assert ok.status_code == 200 and ok.json()["ok"] is True and len(secure.calls) == 1
    assert "S3CRET" not in ok.text


def test_replay_returns_the_stored_answer_and_sends_nothing(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    secure = _FakeSecure(fail=RuntimeError("timeout"))
    client = _client(harness, monkeypatch, secure, **ON)
    url = "/v1/polymarket/write/notifications.drop"
    first = client.post(url, json={"ids": "1"}, headers={"Idempotency-Key": "k4", **_stepup(client)})
    assert first.json()["outcome"] == "UNKNOWN" and first.json()["ok"] is False
    again = client.post(url, json={"ids": "1"}, headers={"Idempotency-Key": "k4"})
    assert again.json()["replayed"] is True and again.json()["outcome"] == "UNKNOWN"
    assert len(secure.calls) == 1  # UNKNOWN is never resent


def test_typed_text_is_checked_over_http_and_attempts_are_audited(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    secure = _FakeSecure()
    client = _client(harness, monkeypatch, secure, **ON)
    url = "/v1/polymarket/write/keys.api.delete"
    bad = client.post(url, json={"confirm": "no"}, headers={"Idempotency-Key": "k5", **_stepup(client)})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "ack_required" and secure.calls == []
    good = client.post(url, json={"confirm": "DELETE API KEY"}, headers={"Idempotency-Key": "k6", **_stepup(client)})
    assert good.json()["outcome"] == "OK" and secure.calls[0][0] == "delete_api_key"
    actions = {row["action"] for row in harness.store.recent_audit(50)}
    assert "polymarket.write.keys.api.delete" in actions


def test_heartbeat_routes_are_off_by_default_and_need_the_text(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    secure = _FakeSecure()
    off = _client(harness, monkeypatch, secure)
    res = off.post("/v1/polymarket/heartbeat/start", json={"confirm": HEARTBEAT_ACK})
    assert res.status_code == 403 and res.json()["error"]["code"] == "write_disabled" and secure.calls == []
    on = _client(harness, monkeypatch, secure, **ON)
    no_text = on.post("/v1/polymarket/heartbeat/start", json={"confirm": "x"}, headers=_stepup(on))
    assert no_text.status_code == 400 and secure.calls == []
    stop = on.post("/v1/polymarket/heartbeat/stop", json={})
    assert stop.status_code == 200 and stop.json()["running"] is False


def test_capabilities_heartbeat_follows_the_flags(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    off = _client(harness, monkeypatch, _FakeSecure())
    assert off.get("/v1/capabilities").json()["heartbeat"]["enabled"] is False
    on = _client(harness, monkeypatch, _FakeSecure(), **ON)
    assert on.get("/v1/capabilities").json()["heartbeat"]["enabled"] is True
