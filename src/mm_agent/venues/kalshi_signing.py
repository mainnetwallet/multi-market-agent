"""Kalshi request signing. The ONLY module allowed to read the Kalshi private key."""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa


class KalshiSigner:
    def __init__(self, api_key_id: str, private_key_path: str) -> None:
        if not api_key_id or not private_key_path:
            raise ValueError("Kalshi API key id and private key path are required")
        path = Path(private_key_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Kalshi private key file not found: {path.name}")
        key_data = path.read_bytes()
        key = serialization.load_pem_private_key(key_data, password=None)
        self._key: rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey
        if isinstance(key, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)):
            self._key = key
        else:
            raise ValueError("Unsupported Kalshi private key type (need RSA or Ed25519)")
        self.api_key_id = api_key_id

    @classmethod
    def from_env(cls, api_key_id: str) -> KalshiSigner:
        return cls(api_key_id=api_key_id, private_key_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH", ""))

    def _sign_message(self, message: bytes) -> bytes:
        if isinstance(self._key, ed25519.Ed25519PrivateKey):
            return self._key.sign(message)
        return self._key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )

    def headers(self, method: str, full_path: str, *, timestamp_ms: str | None = None) -> dict[str, str]:
        timestamp = timestamp_ms or str(int(time.time() * 1000))
        path_without_query = full_path.split("?", 1)[0]
        message = f"{timestamp}{method.upper()}{path_without_query}".encode()
        signature = base64.b64encode(self._sign_message(message)).decode()
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }


def redacted_signer_info(signer: KalshiSigner) -> dict[str, Any]:
    return {"api_key_id_suffix": signer.api_key_id[-6:], "algorithm": type(signer._key).__name__}
