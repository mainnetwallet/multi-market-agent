"""The funds signer: an ERC-20 transfer of the venue's collateral to the bridge.

The ONLY module allowed to read ``FUNDS_SIGNER_PRIVATE_KEY``. It is a separate
key from every trading key; see SECURITY.md for the custody tradeoff.

Polymarket's withdrawal is: the bridge gives an address for one destination, and
the wallet sends its pUSD to that address. The bridge then converts and delivers
to the recipient. So the transfer is a plain ERC-20 transfer on Polygon.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from eth_account import Account
from eth_account.signers.local import LocalAccount

# pUSD on Polygon (chain 137), 6 decimals.
POLYGON_CHAIN_ID = 137
POLYGON_PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"


class SignerError(RuntimeError):
    """The transfer could not be signed or sent."""


@dataclass(frozen=True)
class TransferReceipt:
    tx_hash: str
    from_address: str
    to_address: str
    amount_base_units: int


def _private_key() -> str:
    key = os.environ.get("FUNDS_SIGNER_PRIVATE_KEY", "").strip()
    if not key:
        raise SignerError("FUNDS_SIGNER_PRIVATE_KEY is not set")
    return key


def signer_address() -> str:
    return str(Account.from_key(_private_key()).address)


def _erc20_transfer_data(to: str, amount_base_units: int) -> str:
    """abi.encodeWithSelector(transfer(address,uint256), to, amount)."""

    if amount_base_units <= 0:
        raise SignerError("refusing to transfer zero")
    selector = "a9059cbb"
    padded_to = to.lower().removeprefix("0x").rjust(64, "0")
    padded_amount = format(amount_base_units, "x").rjust(64, "0")
    return "0x" + selector + padded_to + padded_amount


async def send_erc20_transfer(
    *,
    rpc_url: str,
    to_address: str,
    amount_base_units: int,
    token_address: str = POLYGON_PUSD,
    chain_id: int = POLYGON_CHAIN_ID,
) -> TransferReceipt:
    """Sign and send one ERC-20 transfer. Raises SignerError on any failure.

    The caller must have already checked that the recipient and the chain match
    the withdrawal record; this function does not second-guess the amount.
    """
    import httpx

    account: LocalAccount = Account.from_key(_private_key())
    data = _erc20_transfer_data(to_address, amount_base_units)

    # The nonce must be read from the chain, and the transaction must be sent
    # once. A timeout is UNKNOWN, never a resend.
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        nonce_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getTransactionCount",
            "params": [account.address, "pending"],
        }
        nonce_response = await client.post(rpc_url, json=nonce_payload)
        nonce_response.raise_for_status()
        nonce = int(nonce_response.json()["result"], 16)

        chain_payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "eth_chainId",
            "params": [],
        }
        chain_response = await client.post(rpc_url, json=chain_payload)
        chain_response.raise_for_status()
        on_chain_id = int(chain_response.json()["result"], 16)
        if on_chain_id != chain_id:
            raise SignerError(f"RPC is on chain {on_chain_id}, expected {chain_id}")

        # The gas price comes from the chain, never from a guess.
        gas_payload = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "eth_gasPrice",
            "params": [],
        }
        gas_response = await client.post(rpc_url, json=gas_payload)
        gas_response.raise_for_status()
        gas_price = int(gas_response.json()["result"], 16)
        max_fee_per_gas = gas_price * 2
        max_priority_fee_per_gas = gas_price

        tx: dict[str, Any] = {
            "to": token_address,
            "value": 0,
            "data": data,
            "gas": 120_000,  # an ERC-20 transfer fits well inside this
            "maxFeePerGas": max_fee_per_gas,
            "maxPriorityFeePerGas": max_priority_fee_per_gas,
            "chainId": chain_id,
            "nonce": nonce,
            "type": 2,
        }
        signed = account.sign_transaction(tx)
        raw = signed.raw_transaction

        send_payload = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "eth_sendRawTransaction",
            "params": ["0x" + bytes(raw).hex()],
        }
        send_response = await client.post(rpc_url, json=send_payload)
        body = send_response.json()
        if "error" in body:
            raise SignerError(f"RPC rejected the transfer: {body['error']}")
        tx_hash = body.get("result")
        if not tx_hash:
            raise SignerError("the RPC accepted the transaction but returned no hash")

    return TransferReceipt(
        tx_hash=tx_hash,
        from_address=account.address,
        to_address=to_address,
        amount_base_units=amount_base_units,
    )


def to_base_units(amount: Decimal, decimals: int = 6) -> int:
    return int((amount * (10**decimals)).to_integral_value())
