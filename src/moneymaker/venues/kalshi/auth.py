from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey


class KalshiSigner:
    """Signs f"{timestamp_ms}{METHOD}{path}" with RSA-PSS/SHA-256 or Ed25519, by key type.

    path is the full path from the API root (e.g. /trade-api/v2/markets) without the query string.
    """

    def __init__(self, key_id: str, private_key: RSAPrivateKey | Ed25519PrivateKey) -> None:
        """Stores the key id and an RSA or Ed25519 private key.
        Raises TypeError for any other key type.
        """
        if not isinstance(private_key, RSAPrivateKey | Ed25519PrivateKey):
            raise TypeError(f"unsupported Kalshi key type: {type(private_key).__name__}")
        self.key_id = key_id
        self._key = private_key

    @classmethod
    def from_pem_file(cls, key_id: str, path: Path) -> KalshiSigner:
        """Loads an unencrypted PEM private key from path and returns a signer for it."""
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        return cls(key_id, key)  # type: ignore[arg-type]

    def sign(self, message: bytes) -> bytes:
        """Signs message with Ed25519, or with RSA-PSS SHA-256 for RSA keys."""
        if isinstance(self._key, Ed25519PrivateKey):
            return self._key.sign(message)
        return self._key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )

    def headers(self, method: str, path: str, timestamp_ms: int | None = None) -> dict[str, str]:
        """Returns the KALSHI-ACCESS-* headers for a request.
        The query string is stripped from path before signing.
        """
        ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        path = path.split("?", 1)[0]
        signature = self.sign(f"{ts}{method.upper()}{path}".encode())
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }
