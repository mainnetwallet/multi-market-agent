"""Limitless request HMAC + EIP-712 order signing.

The ONLY module allowed to read the Limitless token secret and wallet key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from datetime import UTC, datetime
from decimal import Decimal

from eth_account import Account
from eth_utils.address import to_checksum_address

CHAIN_ID = 8453
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _config() -> tuple[str, str, str]:
    token_id = os.environ.get("LMTS_TOKEN_ID", "").strip()
    secret = os.environ.get("LMTS_TOKEN_SECRET", "").strip()
    private_key = os.environ.get("LIMITLESS_PRIVATE_KEY", "").strip()
    if not token_id or not secret:
        raise RuntimeError("LMTS_TOKEN_ID and LMTS_TOKEN_SECRET are required")
    if not private_key:
        raise RuntimeError("LIMITLESS_PRIVATE_KEY is required for signed orders")
    return token_id, secret, private_key


def wallet_address() -> str:
    _, _, private_key = _config()
    return str(Account.from_key(private_key).address)


def wallet_address_or_empty() -> str:
    """The trading wallet's public address, or "" when no usable key is set. Never returns the key.

    Needs only `LIMITLESS_PRIVATE_KEY`, not the API token, so the deposit address can be shown before the
    token is configured.
    """
    private_key = os.environ.get("LIMITLESS_PRIVATE_KEY", "").strip()
    if not private_key:
        return ""
    try:
        return str(Account.from_key(private_key).address)
    except ValueError:
        return ""


def auth_headers(method: str, path_with_query: str, body: str = "") -> dict[str, str]:
    token_id, secret, _ = _config()
    timestamp = datetime.now(UTC).isoformat()
    message = f"{timestamp}\n{method.upper()}\n{path_with_query}\n{body}"
    signature = base64.b64encode(hmac.new(base64.b64decode(secret), message.encode(), hashlib.sha256).digest()).decode()
    return {
        "lmts-api-key": token_id,
        "lmts-timestamp": timestamp,
        "lmts-signature": signature,
    }


def ws_auth_headers() -> dict[str, str]:
    """Headers that sign the Socket.IO handshake (fixed path, empty body), fresh on every connection.

    Needs only the API token, not the wallet key: the stream reads, it never trades.
    """

    token_id = os.environ.get("LMTS_TOKEN_ID", "").strip()
    secret = os.environ.get("LMTS_TOKEN_SECRET", "").strip()
    if not token_id or not secret:
        raise RuntimeError("LMTS_TOKEN_ID and LMTS_TOKEN_SECRET are required")
    timestamp = datetime.now(UTC).isoformat()
    message = f"{timestamp}\nGET\n/socket.io/?EIO=4&transport=websocket\n"
    signature = base64.b64encode(hmac.new(base64.b64decode(secret), message.encode(), hashlib.sha256).digest()).decode()
    return {"lmts-api-key": token_id, "lmts-timestamp": timestamp, "lmts-signature": signature}


def sign_order(
    *,
    salt: str,
    maker: str,
    token_id: str,
    maker_amount: int,
    taker_amount: int,
    fee_rate_bps: int,
    side: int,
    verifying_contract: str,
    expiration: str = "0",
) -> str:
    _, _, private_key = _config()
    message = {
        "salt": int(salt),
        "maker": to_checksum_address(maker),
        "signer": to_checksum_address(maker),
        "taker": ZERO_ADDRESS,
        "tokenId": int(token_id),
        "makerAmount": maker_amount,
        "takerAmount": taker_amount,
        "expiration": int(expiration),
        "nonce": 0,
        "feeRateBps": int(fee_rate_bps),
        "side": side,
        "signatureType": 0,
    }
    typed_data = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": [
                {"name": "salt", "type": "uint256"},
                {"name": "maker", "type": "address"},
                {"name": "signer", "type": "address"},
                {"name": "taker", "type": "address"},
                {"name": "tokenId", "type": "uint256"},
                {"name": "makerAmount", "type": "uint256"},
                {"name": "takerAmount", "type": "uint256"},
                {"name": "expiration", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
                {"name": "feeRateBps", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "signatureType", "type": "uint8"},
            ],
        },
        "primaryType": "Order",
        "domain": {
            "name": "Limitless CTF Exchange",
            "version": "1",
            "chainId": CHAIN_ID,
            "verifyingContract": to_checksum_address(verifying_contract),
        },
        "message": message,
    }
    signed = Account.sign_typed_data(private_key, full_message=typed_data)
    return "0x" + bytes(signed.signature).hex()


def salt() -> str:
    return str(int(time.time() * 1_000_000))


def size_to_units(value: Decimal) -> int:
    return int((value * Decimal("1000000")).to_integral_value())
