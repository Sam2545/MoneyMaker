from __future__ import annotations

import time
from collections.abc import Sequence

import httpx
import numpy as np
import structlog

from moneymaker.backoff import backoff_delay

log = structlog.get_logger(__name__)

API_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "nvidia/nemotron-3.5-embed-8b:free"
BATCH_SIZE = 128
FREE_MIN_INTERVAL_S = 3.1
RETRY_STATUSES = {408, 429, 500, 502, 503, 504, 524, 529}


class OpenRouterEncoder:
    """Encodes texts with an OpenRouter embedding model, in batches, with retries and pacing.

    Models ending in ":free" are paced to at most one request every FREE_MIN_INTERVAL_S seconds.
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        base_url: str = API_URL,
        batch_size: int = BATCH_SIZE,
        timeout_s: float = 120.0,
        max_attempts: int = 6,
    ) -> None:
        """Creates an authenticated client for model."""
        self.model = model
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.min_interval = FREE_MIN_INTERVAL_S if model.endswith(":free") else 0.0
        self._last = 0.0
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}", "X-Title": "MoneyMaker"},
        )

    def _post(self, texts: Sequence[str]) -> list[list[float]]:
        """Embeds one batch and returns vectors in input order."""
        for attempt in range(self.max_attempts):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            resp = self._http.post("/embeddings", json={"model": self.model, "input": list(texts)})
            if resp.status_code in RETRY_STATUSES and attempt < self.max_attempts - 1:
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after else max(2.0, backoff_delay(attempt, 2.0))
                log.warning("openrouter.retry", status=resp.status_code, delay=round(delay, 1))
                time.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"OpenRouter embeddings {resp.status_code}: {resp.text[:300]}",
                    request=resp.request,
                    response=resp,
                )
            data = sorted(resp.json()["data"], key=lambda d: d.get("index", 0))
            if len(data) != len(texts):
                raise ValueError(f"expected {len(texts)} embeddings, got {len(data)}")
            return [d["embedding"] for d in data]
        raise AssertionError("unreachable")

    def __call__(self, texts: Sequence[str]) -> np.ndarray:
        """Returns a float32 matrix with one embedding row per text."""
        rows: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            rows.extend(self._post(batch))
            log.info("openrouter.embedded", done=min(i + self.batch_size, len(texts)),
                     total=len(texts))
        return np.asarray(rows, dtype=np.float32)

    def close(self) -> None:
        """Closes the underlying HTTP client."""
        self._http.close()
