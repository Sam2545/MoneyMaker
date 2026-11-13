from __future__ import annotations

import re
import time
from collections.abc import Sequence

import httpx
import numpy as np
import structlog

from moneymaker.backoff import backoff_delay

log = structlog.get_logger(__name__)

API_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-embedding-2"
DIMENSIONS = 768
BATCH_SIZE = 50
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
SIMILARITY_PREFIX = "task: sentence similarity | query: "
MAX_RETRY_WAIT_S = 120.0


def retry_delay(resp: httpx.Response) -> float | None:
    """Returns the server-suggested retry delay in seconds from a Gemini error, if present."""
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', resp.text)
    return float(m.group(1)) + 1.0 if m else None


class GeminiEncoder:
    """Encodes texts with a Gemini embedding model for symmetric similarity, in batches.

    gemini-embedding-2 takes the task as a text prefix; gemini-embedding-001 takes
    task_type SEMANTIC_SIMILARITY.
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        dimensions: int = DIMENSIONS,
        base_url: str = API_URL,
        batch_size: int = BATCH_SIZE,
        timeout_s: float = 120.0,
        max_attempts: int = 10,
    ) -> None:
        """Creates an authenticated client for model."""
        self.model = model
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self._http = httpx.Client(
            base_url=base_url, timeout=timeout_s, headers={"x-goog-api-key": api_key}
        )

    def _request(self, text: str) -> dict:
        """Returns one batchEmbedContents request entry for text."""
        entry: dict = {
            "model": f"models/{self.model}",
            "content": {"parts": [{"text": text}]},
            "output_dimensionality": self.dimensions,
        }
        if self.model == "gemini-embedding-001":
            entry["task_type"] = "SEMANTIC_SIMILARITY"
        else:
            entry["content"]["parts"][0]["text"] = SIMILARITY_PREFIX + text
        return entry

    def _post(self, texts: Sequence[str]) -> list[list[float]]:
        """Embeds one batch and returns vectors in input order."""
        body = {"requests": [self._request(t) for t in texts]}
        path = f"/models/{self.model}:batchEmbedContents"
        for attempt in range(self.max_attempts):
            resp = self._http.post(path, json=body)
            if resp.status_code in RETRY_STATUSES and attempt < self.max_attempts - 1:
                delay = retry_delay(resp) or max(2.0, backoff_delay(attempt, 2.0, cap=60.0))
                if delay > MAX_RETRY_WAIT_S:
                    raise httpx.HTTPStatusError(
                        f"Gemini quota exhausted; retry suggested in {delay:.0f}s",
                        request=resp.request,
                        response=resp,
                    )
                log.warning("gemini.retry", status=resp.status_code, delay=round(delay, 1))
                time.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"Gemini embeddings {resp.status_code}: {resp.text[:300]}",
                    request=resp.request,
                    response=resp,
                )
            vectors = [e["values"] for e in resp.json()["embeddings"]]
            if len(vectors) != len(texts):
                raise ValueError(f"expected {len(texts)} embeddings, got {len(vectors)}")
            return vectors
        raise AssertionError("unreachable")

    def __call__(self, texts: Sequence[str]) -> np.ndarray:
        """Returns a float32 matrix with one embedding row per text."""
        rows: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            rows.extend(self._post(texts[i : i + self.batch_size]))
            log.info("gemini.embedded", done=min(i + self.batch_size, len(texts)), total=len(texts))
        return np.asarray(rows, dtype=np.float32)

    def close(self) -> None:
        """Closes the underlying HTTP client."""
        self._http.close()
