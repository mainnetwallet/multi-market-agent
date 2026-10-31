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


EVM_USDC_ETH = "0x" + "a1" * 20
EVM_TUSD = "0x" + "b2" * 20
EVM_USDC_POLY = "0x" + "c3" * 20
EVM_USDCE_POLY = "0x" + "d4" * 20
EVM_USDC_BASE = "0x" + "e5" * 20
SOL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TRON_USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def _multi_chain_assets() -> list[dict]:
    def asset(chain_id: str, chain: str, symbol: str, address: str, minimum: int) -> dict:
        return {
            "chainId": chain_id,
            "chainName": chain,
            "token": {"name": symbol, "symbol": symbol, "address": address, "decimals": 6},
            "minCheckoutUsd": minimum,
        }

    # The names and ids here are test data only; the code under test must not know any of them.
    return [
        asset("c1", "Chain One", "TUSD", EVM_TUSD, 7),
        asset("c1", "Chain One", "USDC", EVM_USDC_ETH, 7),
        asset("c2", "Chain Two", "USDC", SOL_USDC, 2),
        asset("c3", "Chain Three", "USDC", EVM_USDC_POLY, 2),
        asset("c3", "Chain Three", "USDC.e", EVM_USDCE_POLY, 2),
        asset("c4", "Chain Four", "USDC", EVM_USDC_BASE, 2),
        asset("c5", "Chain Five", "USDT", TRON_USDT, 9),
        asset("c6", "Chain Six", "XCOIN", "native", 9),
    ]


def _deposit_client(harness: Harness, bridge: object) -> TestClient:
    bridge.assets = _multi_chain_assets()  # type: ignore[attr-defined]
    harness.settings.__dict__["funds_enabled"] = True
    harness.settings.__dict__["polymarket_wallet_address"] = "0x" + "ab" * 20
    return make_client(harness)


def test_deposit_options_come_from_the_bridge_list_in_a_data_derived_order(harness: Harness, bridge: object) -> None:
    client = _deposit_client(harness, bridge)
    body = client.get("/v1/funds/polymarket/deposit/options").json()
    # Lowest minimum first, then more tokens, then the bridge's own order.
    assert [c["chain_id"] for c in body["chains"]] == ["c3", "c2", "c4", "c1", "c5", "c6"]
    # Tokens listed on the most chains first: USDC (4 chains) before USDC.e (1).
    assert [t["symbol"] for t in body["chains"][0]["tokens"]] == ["USDC", "USDC.e"]
    assert [t["symbol"] for t in body["chains"][3]["tokens"]] == ["USDC", "TUSD"]
    assert body["default"] == {"chain_id": "c3", "token": EVM_USDC_POLY}


def test_deposit_default_is_not_just_the_first_listed_asset(harness: Harness, bridge: object) -> None:
    client = _deposit_client(harness, bridge)
    body = client.get("/v1/funds/polymarket/deposit").json()
    assert (body["network"], body["asset"], body["min_amount"]) == ("Chain Three", "USDC", "2")


def test_deposit_address_type_is_derived_from_the_data(harness: Harness, bridge: object) -> None:
    from tests.conftest import BRIDGE_ADDRESS, BRIDGE_BTC_ADDRESS, BRIDGE_SVM_ADDRESS, BRIDGE_TRON_ADDRESS

    client = _deposit_client(harness, bridge)

    def pick(network: str, **extra: str) -> dict:
        return client.get("/v1/funds/polymarket/deposit", params={"network": network, **extra}).json()

    assert pick("c3")["address"] == BRIDGE_ADDRESS
    assert pick("c1", token=EVM_TUSD.upper().replace("0X", "0x"))["asset"] == "TUSD"
    sol = pick("c2")
    assert (sol["address_type"], sol["address"]) == ("svm", BRIDGE_SVM_ADDRESS)
    # Tron's token looks like a Solana address too; it is resolved because svm is already taken.
    tron = pick("c5")
    assert (tron["address_type"], tron["address"]) == ("tron", BRIDGE_TRON_ADDRESS)
    # A token with no recognisable address format gets the one key nobody else uses.
    btc = pick("c6")
    assert (btc["address_type"], btc["address"]) == ("btc", BRIDGE_BTC_ADDRESS)


def test_deposit_refuses_a_chain_it_cannot_place_for_certain(harness: Harness, bridge: object) -> None:
    client = _deposit_client(harness, bridge)
    bridge.assets.append(  # type: ignore[attr-defined]
        {
            "chainId": "c7",
            "chainName": "Chain Seven",
            "token": {"name": "Y", "symbol": "Y", "address": "mystery", "decimals": 6},
            "minCheckoutUsd": 3,
        }
    )
    body = client.get("/v1/funds/polymarket/deposit", params={"network": "c7"}).json()
    assert body["supported"] is False and "with certainty" in body["reason"]
    # The other unknown chain is refused as well rather than guessed: two chains, one free key.
    assert client.get("/v1/funds/polymarket/deposit", params={"network": "c6"}).json()["supported"] is False


NATIVE_MARKER = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
BTC_MARKER = "bc1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqmql8k8"


def _live_shaped_assets() -> list[dict]:
    """The shapes the live /supported-assets list has: native markers on every chain, a Bitcoin-format
    token on both Bitcoin and Lightning, and pseudo-chains with only a zero address."""

    def asset(chain_id: str, chain: str, symbol: str, address: str, minimum: float = 2) -> dict:
        return {
            "chainId": chain_id,
            "chainName": chain,
            "token": {"name": symbol, "symbol": symbol, "address": address, "decimals": 6},
            "minCheckoutUsd": minimum,
        }

    return [
        asset("c1", "Chain One", "ETH", NATIVE_MARKER),
        asset("c1", "Chain One", "USDC", EVM_USDC_BASE),
        asset("c2", "Chain Two", "SOL", NATIVE_MARKER),
        asset("c2", "Chain Two", "SOL", "1" * 32),
        asset("c2", "Chain Two", "USDC", SOL_USDC),
        asset("c3", "Chain Three", "TRX", NATIVE_MARKER),
        asset("c3", "Chain Three", "USDT", TRON_USDT),
        asset("c4", "Chain Four", "BTC", NATIVE_MARKER, 3.6),
        asset("c4", "Chain Four", "BTC", BTC_MARKER, 3.6),
        asset("c5", "Chain Five", "BTC", NATIVE_MARKER),
        asset("c5", "Chain Five", "BTC", BTC_MARKER),
        asset("c6", "Chain Six", "USDC", "0x" + "0" * 32),
        asset("c7", "Chain Seven", "USDG", "0x" + "0" * 40),
    ]


def test_deposit_native_markers_do_not_hide_the_real_address_type(harness: Harness, bridge: object) -> None:
    from tests.conftest import BRIDGE_ADDRESS, BRIDGE_SVM_ADDRESS, BRIDGE_TRON_ADDRESS

    client = _deposit_client(harness, bridge)
    bridge.assets = _live_shaped_assets()  # type: ignore[attr-defined]

    def pick(network: str) -> dict:
        return client.get("/v1/funds/polymarket/deposit", params={"network": network}).json()

    assert pick("c1")["address"] == BRIDGE_ADDRESS
    sol = pick("c2")
    assert (sol["address_type"], sol["address"]) == ("svm", BRIDGE_SVM_ADDRESS)
    tron = pick("c3")
    assert (tron["address_type"], tron["address"]) == ("tron", BRIDGE_TRON_ADDRESS)


def test_deposit_never_shows_an_evm_address_for_a_bitcoin_format_chain(harness: Harness, bridge: object) -> None:
    from tests.conftest import BRIDGE_ADDRESS

    client = _deposit_client(harness, bridge)
    bridge.assets = _live_shaped_assets()  # type: ignore[attr-defined]
    # c4 and c5 list the same Bitcoin-format token, so the list cannot say which one the btc key serves:
    # both are refused, and neither is ever given the evm address.
    for network in ("c4", "c5"):
        body = client.get("/v1/funds/polymarket/deposit", params={"network": network}).json()
        assert body["supported"] is False and body["address"] != BRIDGE_ADDRESS


def test_deposit_refuses_a_chain_that_has_only_marker_addresses(harness: Harness, bridge: object) -> None:
    client = _deposit_client(harness, bridge)
    bridge.assets = _live_shaped_assets()  # type: ignore[attr-defined]
    for network in ("c6", "c7"):
        body = client.get("/v1/funds/polymarket/deposit", params={"network": network}).json()
        assert body["supported"] is False and "with certainty" in body["reason"]


def test_deposit_does_not_give_tron_the_solana_address_when_the_bridge_has_no_tron_key(
    harness: Harness, bridge: object
) -> None:
    from tests.conftest import BRIDGE_SVM_ADDRESS

    client = _deposit_client(harness, bridge)
    bridge.assets = _live_shaped_assets()  # type: ignore[attr-defined]
    bridge.deposit_keys = {"evm", "svm", "btc"}  # type: ignore[attr-defined]
    body = client.get("/v1/funds/polymarket/deposit", params={"network": "c3"}).json()
    assert body["supported"] is False and body["address"] != BRIDGE_SVM_ADDRESS
    assert client.get("/v1/funds/polymarket/deposit", params={"network": "c2"}).json()["address"] == BRIDGE_SVM_ADDRESS


def test_deposit_options_leave_out_chains_that_would_be_refused(harness: Harness, bridge: object) -> None:
    client = _deposit_client(harness, bridge)
    bridge.assets = _live_shaped_assets()  # type: ignore[attr-defined]
    body = client.get("/v1/funds/polymarket/deposit/options").json()
    assert sorted(c["chain_id"] for c in body["chains"]) == ["c1", "c2", "c3"]


def test_deposit_rejects_a_token_that_is_not_on_that_chain(harness: Harness, bridge: object) -> None:
    client = _deposit_client(harness, bridge)
    bad = client.get("/v1/funds/polymarket/deposit", params={"network": "c4", "asset": "TUSD"}).json()
    assert bad["supported"] is False


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
