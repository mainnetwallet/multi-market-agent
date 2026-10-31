"""Polymarket signer/client isolation.

The private key is read ONLY here. Adapters receive a client object and never
see the key or L2 credentials.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from polymarket import ApiKeyCreds, AsyncSecureClient

_lock = asyncio.Lock()
_client: AsyncSecureClient | None = None


def credentials_from_env() -> ApiKeyCreds | None:
    key = os.environ.get("POLYMARKET_API_KEY", "").strip()
    secret = os.environ.get("POLYMARKET_API_SECRET", "").strip()
    passphrase = os.environ.get("POLYMARKET_API_PASSPHRASE", "").strip()
    if key and secret and passphrase:
        return ApiKeyCreds.model_validate({"apiKey": key, "secret": secret, "passphrase": passphrase})
    return None


async def secure_client(wallet: str) -> AsyncSecureClient:
    """Create (and cache) the official SDK secure client from env secrets."""

    global _client
    async with _lock:
        if _client is not None:
            return _client
        private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
        if not private_key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY is not set")
        kwargs: dict[str, Any] = {"private_key": private_key}
        if wallet:
            kwargs["wallet"] = wallet
        creds = credentials_from_env()
        if creds is not None:
            kwargs["credentials"] = creds
        _client = await AsyncSecureClient.create(**kwargs)
        return _client


async def close_client() -> None:
    global _client
    async with _lock:
        if _client is not None:
            await _client.close()
            _client = None


def is_configured() -> bool:
    return bool(os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip())


async def resolve_wallet(configured: str = "") -> str:
    """The Polymarket wallet the account trades from.

    An explicit POLYMARKET_WALLET_ADDRESS wins. Without one, the official SDK's own answer is used: the
    signer's Deposit Wallet, which is the wallet polymarket.com shows for this key. Only the public
    address leaves this module. Empty when there is no key or the client cannot be created.
    """

    configured = configured.strip()
    if configured:
        return configured
    if not is_configured():
        return ""
    try:
        client = await secure_client("")
    except Exception:
        return ""
    return str(client.wallet or "")
