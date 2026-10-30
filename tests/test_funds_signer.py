"""The funds signer: ERC-20 transfer encoding and the one-shot send rule."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from mm_agent.funds.signer import (
    SignerError,
    _erc20_transfer_data,
    send_erc20_transfer,
    to_base_units,
)

TEST_KEY = "0x" + "11" * 32


def test_erc20_transfer_data_matches_the_selector() -> None:
    data = _erc20_transfer_data("0x1111111111111111111111111111111111111111", 1_000_000)
    assert data.startswith("0xa9059cbb")  # transfer(address,uint256)
    # The recipient is left-padded to 32 bytes and the amount to 32 bytes.
    assert data[10:74] == "0" * 24 + "11" * 20
    amount_word = data[74:138]
    assert len(amount_word) == 64
    assert int(amount_word, 16) == 1_000_000
    assert len(data) == 138  # 4-byte selector + two 32-byte words


def test_zero_amount_is_refused() -> None:
    with pytest.raises(SignerError, match="zero"):
        _erc20_transfer_data("0x1111111111111111111111111111111111111111", 0)


def test_base_units_use_six_decimals() -> None:
    assert to_base_units(Decimal("1.5")) == 1_500_000
    assert to_base_units(Decimal("0.000001")) == 1


async def test_a_missing_signer_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FUNDS_SIGNER_PRIVATE_KEY", raising=False)
    with pytest.raises(SignerError, match="FUNDS_SIGNER_PRIVATE_KEY"):
        await send_erc20_transfer(
            rpc_url="http://127.0.0.1:1",
            to_address="0x1111111111111111111111111111111111111111",
            amount_base_units=1,
        )


class _FakeRPC:
    """A JSON-RPC stub that records the calls it received."""

    def __init__(self, send_result: str = "0xabc", error: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.send_result = send_result
        self.error = error

    async def post(self, url: str, json: dict) -> httpx.Response:
        self.calls.append(json)
        method = json["method"]
        if method == "eth_getTransactionCount":
            result = "0x5"
        elif method == "eth_chainId":
            result = "0x89"  # Polygon
        elif method == "eth_gasPrice":
            result = "0x3b9aca00"  # 1 gwei
        else:
            if self.error:
                return _response({"jsonrpc": "2.0", "id": json["id"], "error": self.error})
            return _response({"jsonrpc": "2.0", "id": json["id"], "result": self.send_result})
        return _response({"jsonrpc": "2.0", "id": json["id"], "result": result})


@pytest.fixture
def signer_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUNDS_SIGNER_PRIVATE_KEY", TEST_KEY)


async def test_a_transfer_is_signed_and_sent_once(monkeypatch: pytest.MonkeyPatch, signer_key: None) -> None:
    fake = _FakeRPC()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client(fake))

    receipt = await send_erc20_transfer(
        rpc_url="http://rpc.invalid",
        to_address="0x1111111111111111111111111111111111111111",
        amount_base_units=1_000_000,
    )
    assert receipt.tx_hash == "0xabc"
    assert len(receipt.from_address) == 42
    methods = [call["method"] for call in fake.calls]
    assert methods == [
        "eth_getTransactionCount",
        "eth_chainId",
        "eth_gasPrice",
        "eth_sendRawTransaction",
    ]
    # Exactly one transaction was sent.
    assert methods.count("eth_sendRawTransaction") == 1


async def test_a_rejected_transfer_raises_and_sends_nothing(monkeypatch: pytest.MonkeyPatch, signer_key: None) -> None:
    fake = _FakeRPC(error={"code": -32000, "message": "insufficient funds"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client(fake))

    with pytest.raises(SignerError, match="rejected"):
        await send_erc20_transfer(
            rpc_url="http://rpc.invalid",
            to_address="0x1111111111111111111111111111111111111111",
            amount_base_units=1_000_000,
        )


async def test_a_wrong_chain_is_refused_before_sending(monkeypatch: pytest.MonkeyPatch, signer_key: None) -> None:
    fake = _FakeRPC()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client(fake))

    with pytest.raises(SignerError, match="chain"):
        await send_erc20_transfer(
            rpc_url="http://rpc.invalid",
            to_address="0x1111111111111111111111111111111111111111",
            amount_base_units=1_000_000,
            chain_id=8453,  # Base, while the stub answers Polygon
        )
    assert [c["method"] for c in fake.calls].count("eth_sendRawTransaction") == 0


class _Client:
    """A tiny stand-in for httpx.AsyncClient."""

    def __init__(self, fake: _FakeRPC) -> None:
        self._fake = fake

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, json: dict) -> httpx.Response:
        return await self._fake.post(url, json)


def _response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "http://rpc.invalid"))
