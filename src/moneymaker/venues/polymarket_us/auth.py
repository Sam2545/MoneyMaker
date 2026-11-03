from __future__ import annotations

import base64
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class PolymarketUSSigner:
    """Signs requests with Ed25519 over f"{timestamp_ms}{METHOD}{path}" (query string excluded)."""

    def __init__(self, key_id: str, secret_key_b64: str) -> None:
        raw = base64.b64decode(secret_key_b64)
        if len(raw) == 64:
            raw = raw[:32]
        if len(raw) != 32:
            raise ValueError("Polymarket US secret key must decode to 32 or 64 bytes")
        self.key_id = key_id
        self._key = Ed25519PrivateKey.from_private_bytes(raw)

    def headers(self, method: str, path: str, timestamp_ms: int | None = None) -> dict[str, str]:
        ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        signature = self._key.sign(f"{ts}{method.upper()}{path}".encode())
        return {
            "X-PM-Access-Key": self.key_id,
            "X-PM-Timestamp": ts,
            "X-PM-Signature": base64.b64encode(signature).decode(),
        }
