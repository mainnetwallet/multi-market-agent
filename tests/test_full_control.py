"""Full-control backend: idempotency, step-up, manual trading, ceilings, funds.

Everything runs against the FakeVenue, so no real money is ever involved.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from mm_agent.control_api import create_app
from mm_agent.domain import VenueId
from tests.conftest import Harness


def make_client(harness: Harness) -> TestClient:
    app = create_app(harness.settings, harness.agent, harness.scanner, harness.registry, harness.chat, harness.bus)
    client = TestClient(app)
    client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
    return client


# ── capabilities ────────────────────────────────────────────────────────────


def test_capabilities_report_manual_orders_and_venue_order_types(harness: Harness) -> None:
    client = make_client(harness)
    body = client.get("/v1/capabilities").json()
    assert body["manual_orders"] is True
    assert body["cancel"] is True
    fake = body["venues"]["fake"]
    assert "GTC" in fake["order_types"]
    assert fake["post_only"] is True
    assert body["heartbeat"]["enabled"] is False


def test_status_carries_capabilities(harness: Harness) -> None:
    client = make_client(harness)
    body = client.get("/v1/status").json()
    assert "capabilities" in body
    assert body["limits"]["hard_ceilings"]["max_trade_usd"] == "2"


def test_balance_is_read_from_the_venue(harness: Harness) -> None:
    client = make_client(harness)
    body = client.get("/v1/balance?venue=fake").json()
    assert body["venue"] == "fake"
    assert body["mode"] == "LIVE"
    assert body["reason"] is None
    assert Decimal(body["available"]) > 0


# ── idempotency ─────────────────────────────────────────────────────────────


def test_money_moving_post_requires_an_idempotency_key(harness: Harness) -> None:
    client = make_client(harness)
    response = client.post(
        "/v1/orders/limit",
        json={"market_id": "fed-cut", "outcome": "YES", "side": "BUY", "price": "0.43", "shares": "4"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_required"


def test_idempotent_replay_returns_the_first_result(harness: Harness) -> None:
    client = make_client(harness)
    payload = {"market_id": "fed-cut", "outcome": "YES", "side": "BUY", "price": "0.43", "shares": "4"}
    headers = {"Idempotency-Key": "intent-1"}
    first = client.post("/v1/orders/limit", json=payload, headers=headers)
    assert first.status_code == 200, first.text
    second = client.post("/v1/orders/limit", json=payload, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    assert second.json()["order_id"] == first.json()["order_id"]
    # Only one order was actually placed.
    assert len(harness.store.list_orders(venue=VenueId.FAKE)) == 1


def test_same_key_with_a_different_body_is_not_a_replay(harness: Harness) -> None:
    client = make_client(harness)
    headers = {"Idempotency-Key": "intent-2"}
    first = client.post(
        "/v1/orders/limit",
        json={"market_id": "fed-cut", "outcome": "YES", "side": "BUY", "price": "0.43", "shares": "4"},
        headers=headers,
    )
    second = client.post(
        "/v1/orders/limit",
        json={"market_id": "fed-cut", "outcome": "YES", "side": "BUY", "price": "0.45", "shares": "4"},
        headers=headers,
    )
    assert second.json().get("replayed") is not True
    assert second.json()["order_id"] != first.json()["order_id"]


# ── manual trading and risk ─────────────────────────────────────────────────


def test_manual_order_passes_through_and_fills(harness: Harness) -> None:
    client = make_client(harness)
    response = client.post(
        "/v1/orders/limit",
        json={"market_id": "fed-cut", "outcome": "YES", "side": "BUY", "price": "0.45", "shares": "4"},
        headers={"Idempotency-Key": "manual-1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "FILLED"
    assert body["amount_usd"] == "1.80"


def test_manual_order_over_the_cap_is_refused_with_a_reason(harness: Harness) -> None:
    client = make_client(harness)
    response = client.post(
        "/v1/orders/limit",
        json={"market_id": "fed-cut", "outcome": "YES", "side": "BUY", "price": "0.45", "amount_usd": "50"},
        headers={"Idempotency-Key": "manual-2"},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "max_trade_exceeded"
    assert "cap" in body["error"]["message"]


def test_post_only_with_a_kill_order_type_is_refused(harness: Harness) -> None:
    client = make_client(harness)
    response = client.post(
        "/v1/orders/limit",
        json={
            "market_id": "fed-cut",
            "outcome": "YES",
            "side": "BUY",
            "price": "0.45",
            "shares": "5",
            "tif": "FOK",
            "post_only": True,
        },
        headers={"Idempotency-Key": "manual-3"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "post_only_conflict"


def test_order_type_not_offered_by_the_venue_is_refused(harness: Harness) -> None:
    client = make_client(harness)
    # The fake venue has no IOC.
    response = client.post(
        "/v1/orders/limit",
        json={"market_id": "fed-cut", "outcome": "YES", "side": "BUY", "price": "0.45", "shares": "4", "tif": "IOC"},
        headers={"Idempotency-Key": "manual-4"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_order_type"


def test_sell_reduces_a_position(harness: Harness) -> None:
    client = make_client(harness)
    client.post(
        "/v1/orders/limit",
        json={"market_id": "fed-cut", "outcome": "YES", "side": "BUY", "price": "0.45", "shares": "4"},
        headers={"Idempotency-Key": "buy-1"},
    )
    response = client.post(
        "/v1/positions/sell",
        json={"market_id": "fed-cut", "outcome": "YES", "shares": "4"},
        headers={"Idempotency-Key": "sell-1"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["side"] == "SELL"
    assert Decimal(response.json()["filled"]) > 0


def test_sell_more_than_held_is_refused(harness: Harness) -> None:
    client = make_client(harness)
    client.post(
        "/v1/orders/limit",
        json={"market_id": "fed-cut", "outcome": "YES", "side": "BUY", "price": "0.45", "shares": "4"},
        headers={"Idempotency-Key": "buy-2"},
    )
    response = client.post(
        "/v1/positions/sell",
        json={"market_id": "fed-cut", "outcome": "YES", "shares": "99"},
        headers={"Idempotency-Key": "sell-2"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "too_many_shares"


def test_cancel_and_cancel_all_are_never_blocked_by_caps(harness: Harness) -> None:
    client = make_client(harness)
    client.post(
        "/v1/orders/limit",
        json={"market_id": "fed-cut", "outcome": "YES", "side": "BUY", "price": "0.45", "shares": "4"},
        headers={"Idempotency-Key": "rest-1"},
    )
    # Pause the agent: a pause blocks entries, never cancellations.
    client.post("/v1/pause", json={})
    response = client.post("/v1/orders/cancel-all", json={}, headers={"Idempotency-Key": "cancel-all-1"})
    assert response.status_code == 200, response.text
    assert "results" in response.json()


# ── step-up and hard ceilings ───────────────────────────────────────────────


def test_stepup_token_is_single_use(harness: Harness) -> None:
    client = make_client(harness)
    issued = client.post("/v1/auth/stepup", json={"password": "test-password", "action": "ceiling_raise"})
    assert issued.status_code == 200, issued.text
    token = issued.json()["stepup_token"]
    assert issued.json()["expires_in"] == 120
    headers = {"X-Stepup-Token": token}
    body = {"name": "max_trade_usd", "value": "5"}
    first = client.post("/v1/limits/hard-ceiling", json=body, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["value"] == "5"
    # The same token cannot be used again.
    second = client.post("/v1/limits/hard-ceiling", json={"name": "max_trade_usd", "value": "6"}, headers=headers)
    assert second.status_code == 403
    assert second.json()["error"]["code"] == "stepup_invalid"


def test_raising_a_ceiling_without_stepup_is_refused(harness: Harness) -> None:
    client = make_client(harness)
    response = client.post("/v1/limits/hard-ceiling", json={"name": "max_trade_usd", "value": "9"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "stepup_required"


def test_lowering_a_ceiling_needs_no_stepup(harness: Harness) -> None:
    client = make_client(harness)
    response = client.post("/v1/limits/hard-ceiling", json={"name": "max_trade_usd", "value": "1"})
    assert response.status_code == 200, response.text
    assert response.json()["value"] == "1"


def test_raised_ceiling_allows_a_bigger_trade(harness: Harness) -> None:
    client = make_client(harness)
    token = client.post("/v1/auth/stepup", json={"password": "test-password", "action": "ceiling_raise"}).json()[
        "stepup_token"
    ]
    client.post(
        "/v1/limits/hard-ceiling", json={"name": "max_trade_usd", "value": "5"}, headers={"X-Stepup-Token": token}
    )
    response = client.post(
        "/v1/orders/limit",
        json={"market_id": "fed-cut", "outcome": "YES", "side": "BUY", "price": "0.45", "amount_usd": "3"},
        headers={"Idempotency-Key": "big-1"},
    )
    assert response.status_code == 200, response.text


def test_stepup_with_a_wrong_password_is_refused(harness: Harness) -> None:
    client = make_client(harness)
    response = client.post("/v1/auth/stepup", json={"password": "nope", "action": "ceiling_raise"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "bad_password"


# ── funds ───────────────────────────────────────────────────────────────────


def test_funds_routes_are_off_by_default(harness: Harness) -> None:
    client = make_client(harness)
    response = client.get("/v1/funds/polymarket/deposit")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "funds_disabled"


def test_a_withdrawal_to_an_unknown_address_is_refused(harness: Harness, bridge: object) -> None:
    del bridge
    harness.settings.__dict__["funds_enabled"] = True
    client = make_client(harness)
    response = client.get("/v1/funds/polymarket/deposit")
    assert response.status_code == 200
    preview = client.post(
        "/v1/funds/withdraw/preview",
        json={"venue": "polymarket", "amount": "1", "address_id": "whatever"},
    )
    assert preview.status_code == 400
    assert preview.json()["error"]["code"] == "not_allowlisted"


def test_allowlist_rejects_a_bad_address(harness: Harness) -> None:
    harness.settings.__dict__["funds_enabled"] = True
    client = make_client(harness)
    token = client.post("/v1/auth/stepup", json={"password": "test-password", "action": "allowlist_add"}).json()[
        "stepup_token"
    ]
    response = client.post(
        "/v1/funds/allowlist",
        json={"label": "cold", "address": "0xnotanaddress", "network": "base", "asset": "USDC"},
        headers={"X-Stepup-Token": token},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_address"


def test_allowlist_add_and_cooling(harness: Harness) -> None:
    harness.settings.__dict__["funds_enabled"] = True
    client = make_client(harness)
    token = client.post("/v1/auth/stepup", json={"password": "test-password", "action": "allowlist_add"}).json()[
        "stepup_token"
    ]
    added = client.post(
        "/v1/funds/allowlist",
        json={
            "label": "cold storage",
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "network": "base",
            "asset": "USDC",
        },
        headers={"X-Stepup-Token": token},
    )
    assert added.status_code == 200, added.text
    assert added.json()["cooling_hours"] == 24
    entries = client.get("/v1/funds/allowlist").json()["entries"]
    assert len(entries) == 1


def test_a_withdrawal_records_a_decimal_amount(harness: Harness, bridge: object) -> None:
    del bridge
    # The amount is a Decimal and the id is derived from it: it must not reach
    # json.dumps as a Decimal.
    harness.settings.__dict__["funds_enabled"] = True
    harness.settings.__dict__["funds_allowlist_cooling_hours"] = 0
    client = make_client(harness)
    token = client.post("/v1/auth/stepup", json={"password": "test-password", "action": "allowlist_add"}).json()[
        "stepup_token"
    ]
    entry_id = client.post(
        "/v1/funds/allowlist",
        json={
            "label": "cold",
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "network": "base",
            "asset": "USDC",
        },
        headers={"X-Stepup-Token": token},
    ).json()["id"]
    withdraw_token = client.post("/v1/auth/stepup", json={"password": "test-password", "action": "withdraw"}).json()[
        "stepup_token"
    ]
    response = client.post(
        "/v1/funds/withdraw",
        json={"venue": "polymarket", "amount": "1.00", "address_id": entry_id},
        headers={"Idempotency-Key": "wd-1", "X-Stepup-Token": withdraw_token},
    )
    # Whatever the outcome, the request must never crash on the Decimal amount.
    assert response.status_code in (200, 400), response.text


# ── history and market data ─────────────────────────────────────────────────


def test_trades_listing_returns_fills(harness: Harness) -> None:
    client = make_client(harness)
    client.post(
        "/v1/orders/limit",
        json={"market_id": "fed-cut", "outcome": "YES", "side": "BUY", "price": "0.45", "shares": "4"},
        headers={"Idempotency-Key": "trade-1"},
    )
    body = client.get("/v1/trades?venue=fake").json()
    assert body["trades"], body
    row = body["trades"][0]
    assert row["venue"] == "fake"
    assert row["outcome"] == "YES"
    assert row["mode"] == "LIVE"


def test_pnl_daily_series(harness: Harness) -> None:
    client = make_client(harness)
    body = client.get("/v1/pnl/daily?days=7").json()
    assert len(body["days"]) == 7
    assert body["days"][0]["date"]


def test_market_book_and_history(harness: Harness) -> None:
    client = make_client(harness)
    book = client.get("/v1/markets/fed-cut/book?venue=fake&outcome=YES")
    assert book.status_code == 200, book.text
    assert book.json()["asks"]
    history = client.get("/v1/markets/fed-cut/history?venue=fake")
    assert history.status_code == 200, history.text
    assert history.json()["points"]


def test_ws_accepts_a_bearer_token(harness: Harness) -> None:
    client = make_client(harness)
    login = client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
    token = login.json()["token"]
    with client.websocket_connect("/v1/ws", headers={"Authorization": f"Bearer {token}"}) as ws:
        hello = ws.receive_json()
        assert hello["topic"] == "hello"
