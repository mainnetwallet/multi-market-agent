"""Guards on the funds path: destination, daily limit, position cap, client id.

These are the checks that stand between a mistake and real money.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from mm_agent.control_api import create_app
from mm_agent.domain import Outcome, VenueId
from mm_agent.funds import FundsError, FundsService
from tests.conftest import Harness

BRIDGE = "https://bridge.polymarket.com"
BRIDGE_ADDRESS = "0x9999888877776666555544443333222211110000"


def make_client(harness: Harness) -> TestClient:
    app = create_app(harness.settings, harness.agent, harness.scanner, harness.registry, harness.chat, harness.bus)
    client = TestClient(app)
    client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
    return client


class _BridgeStub:
    """A stub for the bridge: supported assets, a quote, and a withdraw address."""

    def __init__(self, assets: list[dict] | None = None, echo: dict | None = None) -> None:
        self.assets = assets or [
            {
                "chainId": "8453",
                "chainName": "Base",
                "token": {"symbol": "USDC", "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"},
                "minCheckoutUsd": 2,
            },
            {
                "chainId": "1",
                "chainName": "Ethereum",
                "token": {"symbol": "USDC", "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"},
                "minCheckoutUsd": 7,
            },
        ]
        self.echo = echo
        self.quotes: list[dict] = []
        self.withdraws: list[dict] = []

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        if str(url).endswith("/supported-assets"):
            return httpx.Response(200, json={"supportedAssets": self.assets}, request=httpx.Request("GET", url))
        return httpx.Response(200, json={"transactions": []}, request=httpx.Request("GET", url))

    async def post(self, url: str, json: dict | None = None, **kwargs: object) -> httpx.Response:
        if str(url).endswith("/quote"):
            self.quotes.append(json or {})
            return httpx.Response(
                200,
                json={"estFeeBreakdown": {"appFeeUsd": 0.25, "gasUsd": 0.1}, "estCheckoutTimeMs": 25000},
                request=httpx.Request("POST", str(url)),
            )
        if str(url).endswith("/withdraw"):
            self.withdraws.append(json or {})
            body: dict = {"address": {"evm": BRIDGE_ADDRESS}}
            if self.echo:
                body.update(self.echo)
            return httpx.Response(201, json=body, request=httpx.Request("POST", str(url)))
        return httpx.Response(200, json={}, request=httpx.Request("POST", str(url)))


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch) -> _BridgeStub:
    stub = _BridgeStub()

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, **kwargs: object) -> httpx.Response:
            return await stub.get(url, **kwargs)

        async def post(self, url: str, json: dict | None = None, **kwargs: object) -> httpx.Response:
            return await stub.post(url, json=json, **kwargs)

    monkeypatch.setattr("mm_agent.funds.httpx.AsyncClient", lambda *a, **k: _Client())
    return stub


def _allowlist(client: TestClient, network: str = "base", asset: str = "USDC") -> str:
    token = client.post("/v1/auth/stepup", json={"password": "test-password", "action": "allowlist_add"}).json()[
        "stepup_token"
    ]
    return client.post(
        "/v1/funds/allowlist",
        json={
            "label": "cold",
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "network": network,
            "asset": asset,
        },
        headers={"X-Stepup-Token": token},
    ).json()["id"]


# ── destination resolution ──────────────────────────────────────────────────


async def test_the_bridge_request_uses_the_chosen_chain_and_token(harness: Harness, bridge: _BridgeStub) -> None:
    harness.settings.__dict__["funds_enabled"] = True
    harness.settings.__dict__["funds_allowlist_cooling_hours"] = 0
    harness.settings.__dict__["live_polymarket"] = True
    client = make_client(harness)
    entry_id = _allowlist(client, network="Ethereum", asset="USDC")

    funds = FundsService(harness.settings, harness.store)
    entry = harness.store.allowlist_get(entry_id)
    assert entry is not None
    await funds._polymarket_quote(Decimal("1"), entry)

    quote = bridge.quotes[-1]
    # Ethereum is chain 1, not the Base default that was hardcoded before.
    assert quote["toChainId"] == "1"
    assert quote["toTokenAddress"] == "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    assert quote["recipientAddress"] == entry["address"]


async def test_an_unsupported_destination_is_refused(harness: Harness, bridge: _BridgeStub) -> None:
    harness.settings.__dict__["funds_enabled"] = True
    harness.settings.__dict__["funds_allowlist_cooling_hours"] = 0
    harness.settings.__dict__["live_polymarket"] = True
    client = make_client(harness)
    entry_id = _allowlist(client, network="Solana", asset="USDC")

    funds = FundsService(harness.settings, harness.store)
    entry = harness.store.allowlist_get(entry_id)
    assert entry is not None
    with pytest.raises(FundsError) as exc:
        await funds._polymarket_quote(Decimal("1"), entry)
    assert exc.value.code == "unsupported_destination"


async def test_a_bridge_answer_for_another_destination_is_refused(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bridge echoed a different chain than the one that was asked for."""

    stub = _BridgeStub(echo={"toChainId": "999"})

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, **kwargs: object) -> httpx.Response:
            return await stub.get(url, **kwargs)

        async def post(self, url: str, json: dict | None = None, **kwargs: object) -> httpx.Response:
            return await stub.post(url, json=json, **kwargs)

    monkeypatch.setattr("mm_agent.funds.httpx.AsyncClient", lambda *a, **k: _Client())
    harness.settings.__dict__["funds_enabled"] = True
    harness.settings.__dict__["live_polymarket"] = True
    client = make_client(harness)
    entry_id = _allowlist(client, network="base", asset="USDC")

    funds = FundsService(harness.settings, harness.store)
    record = {
        "id": "w-test",
        "venue": "polymarket",
        "amount_usd": Decimal("1"),
        "address_id": entry_id,
        "recipient": "0x1234567890abcdef1234567890abcdef12345678",
    }
    with pytest.raises(FundsError) as exc:
        await funds._polymarket_withdraw_address(record)
    assert exc.value.code == "destination_mismatch"


# ── daily limit ─────────────────────────────────────────────────────────────


async def test_the_daily_limit_is_checked_when_the_money_moves(harness: Harness, bridge: _BridgeStub) -> None:
    """The limit must hold at submission time, not only at creation time."""

    harness.settings.__dict__["funds_enabled"] = True
    harness.settings.__dict__["funds_allowlist_cooling_hours"] = 0
    harness.settings.__dict__["live_polymarket"] = True
    harness.settings.__dict__["funds_withdrawal_daily_limit_usd"] = Decimal("25")
    client = make_client(harness)
    entry_id = _allowlist(client)

    funds = FundsService(harness.settings, harness.store)
    withdraw_token = client.post("/v1/auth/stepup", json={"password": "test-password", "action": "withdraw"}).json()[
        "stepup_token"
    ]
    created = client.post(
        "/v1/funds/withdraw",
        json={"venue": "polymarket", "amount": "20", "address_id": entry_id},
        headers={"Idempotency-Key": "wd-1", "X-Stepup-Token": withdraw_token},
    )
    assert created.status_code == 200, created.text

    # Another $20 was already sent today by some earlier withdrawal.
    from datetime import UTC, datetime, timedelta

    harness.store.create_withdrawal(
        venue="polymarket",
        amount_usd=Decimal("20"),
        address_id=entry_id,
        recipient="0x1234567890abcdef1234567890abcdef12345678",
        network="base",
        asset="USDC",
        status="CONFIRMED",
        hold_until=datetime.now(UTC) - timedelta(hours=1),
    )

    # The hold on the new withdrawal ends.
    record = harness.store.list_withdrawals(venue=VenueId.POLYMARKET)[0]
    harness.store.transition_withdrawal(record["id"], "PENDING_HOLD", hold_until=datetime.now(UTC))

    results = await funds.submit_due_withdrawals()
    statuses = [r["status"] for r in results]
    # $20 has already gone today, so this one does not leave.
    assert "FAILED" in statuses
    assert "SUBMITTED" not in statuses


# ── manual order guards ─────────────────────────────────────────────────────


def test_a_long_client_id_is_refused(harness: Harness) -> None:
    """The client id lands in an 80-character column; Postgres rejects a longer one."""

    client = make_client(harness)
    response = client.post(
        "/v1/orders/limit",
        json={
            "market_id": "fed-cut",
            "outcome": "YES",
            "side": "BUY",
            "price": "0.43",
            "shares": "4",
            "client_id": "x" * 100,
        },
        headers={"Idempotency-Key": "long-id"},
    )
    assert response.status_code == 422, response.text


def test_manual_orders_respect_the_position_cap(harness: Harness) -> None:
    """The agent stops at 8 open positions; manual orders must stop there too."""

    # A second market, so the cap is about positions and not about outcomes.
    from mm_agent.domain import Market, MarketStatus, OutcomeInfo

    harness.store.upsert_market(
        Market(
            venue=VenueId.FAKE,
            native_id="other-market",
            title="Another market",
            status=MarketStatus.OPEN,
            tick_size=Decimal("0.01"),
            outcomes=(
                OutcomeInfo(outcome=Outcome.YES, native_id="other-yes", label="Yes"),
                OutcomeInfo(outcome=Outcome.NO, native_id="other-no", label="No"),
            ),
            fee_info={"taker_rate": "0.05"},
        )
    )
    harness.fake.add_market(
        harness.store.get_market(VenueId.FAKE, "other-market"),
        harness.fake.set_book("other-market", Outcome.YES, [("0.40", "1000")], [("0.42", "500")]),
        harness.fake.set_book("other-market", Outcome.NO, [("0.56", "900")], [("0.58", "500")]),
    )

    client = make_client(harness)
    harness.agent.risk.caps["max_open_positions"] = Decimal("1")
    # The trade cap is raised so the position cap is what refuses the second order.
    harness.agent.risk.caps["max_trade_usd"] = Decimal("100")

    first = client.post(
        "/v1/orders/limit",
        json={"market_id": "fed-cut", "outcome": "YES", "side": "BUY", "price": "0.43", "shares": "4"},
        headers={"Idempotency-Key": "pos-1"},
    )
    assert first.status_code == 200, first.text

    # A second market is a second position and is refused.
    second = client.post(
        "/v1/orders/limit",
        json={"market_id": "other-market", "outcome": "YES", "side": "BUY", "price": "0.43", "shares": "4"},
        headers={"Idempotency-Key": "pos-2"},
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "max_open_positions"


def test_the_other_outcome_of_a_held_market_is_not_a_new_position(harness: Harness) -> None:
    """The cap counts markets, not outcomes: YES and NO on one market are one position."""

    client = make_client(harness)
    harness.agent.risk.caps["max_open_positions"] = Decimal("1")
    harness.agent.risk.caps["max_trade_usd"] = Decimal("100")

    first = client.post(
        "/v1/orders/limit",
        json={"market_id": "fed-cut", "outcome": "YES", "side": "BUY", "price": "0.43", "shares": "4"},
        headers={"Idempotency-Key": "same-1"},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/v1/orders/limit",
        json={"market_id": "fed-cut", "outcome": "NO", "side": "BUY", "price": "0.59", "shares": "4"},
        headers={"Idempotency-Key": "same-2"},
    )
    assert second.status_code == 200, second.text


def test_an_expiry_too_soon_is_refused(harness: Harness) -> None:
    """Polymarket needs at least three minutes for a GTD order."""

    from datetime import UTC, datetime, timedelta

    client = make_client(harness)
    response = client.post(
        "/v1/orders/limit",
        json={
            "market_id": "fed-cut",
            "outcome": "YES",
            "side": "BUY",
            "price": "0.43",
            "shares": "4",
            "tif": "GTD",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        },
        headers={"Idempotency-Key": "gtd-1"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "expiry_too_soon"
