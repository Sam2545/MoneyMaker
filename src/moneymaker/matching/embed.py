from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence

import numpy as np

from moneymaker.matching.store import MatchStore
from moneymaker.models import Market

MODEL_NAME = "BAAI/bge-small-en-v1.5"
RULES_CHARS = 300


def market_text(market: Market) -> str:
    """Returns the text embedded for a market: title, YES label and the start of its rules."""
    parts = [market.title, market.yes_label or "", market.rules[:RULES_CHARS]]
    return ". ".join(p.strip() for p in parts if p and p.strip())


def text_hash(text: str) -> str:
    """Returns the SHA-256 hex digest of text."""
    return hashlib.sha256(text.encode()).hexdigest()


class Embedder:
    """Embeds texts with a local fastembed model and caches vectors in a MatchStore."""

    def __init__(
        self,
        store: MatchStore,
        model_name: str = MODEL_NAME,
        encode: Callable[[Sequence[str]], np.ndarray] | None = None,
    ) -> None:
        """Uses encode when given, otherwise loads model_name with fastembed on first use."""
        self.store = store
        self.model_name = model_name
        self._encode = encode
        self.computed = 0

    def _default_encode(self, texts: Sequence[str]) -> np.ndarray:
        """Encodes texts with fastembed, loading the model once."""
        if not hasattr(self, "_model"):
            from fastembed import TextEmbedding

            self._model = TextEmbedding(self.model_name)
        return np.array(list(self._model.embed(list(texts))), dtype=np.float32)

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Returns L2-normalized vectors for texts, computing only those not in the cache."""
        hashes = [text_hash(t) for t in texts]
        cached = self.store.get_embeddings(set(hashes), self.model_name)
        missing = {h: t for h, t in zip(hashes, texts, strict=True) if h not in cached}
        if missing:
            encode = self._encode or self._default_encode
            vectors = encode(list(missing.values()))
            fresh = dict(zip(missing.keys(), vectors, strict=True))
            self.store.put_embeddings(fresh, self.model_name)
            cached.update(fresh)
            self.computed += len(fresh)
        matrix = np.stack([cached[h] for h in hashes]) if hashes else np.zeros((0, 1), np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix / np.where(norms == 0, 1, norms)
