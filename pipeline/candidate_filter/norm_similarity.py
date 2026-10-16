"""Cosine similarity between two norms, based on their action + trigger text.

The norm format has no separate object field: the object is part of `action`,
so the compared text is `action` plus `trigger_event` (when present).
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

import numpy as np


def norm_text(record: dict) -> str:
    """Build the text to embed: action + trigger_event, skipping empty parts."""
    norm = record.get("norm") or {}
    parts = [norm.get("action"), norm.get("trigger_event")]
    return " ; ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())


class NormSimilarity:
    """Embeds norms once (cached by norm_id) and returns cosine similarities.

    `encoder` can be passed in for testing; by default a sentence-transformers
    model is loaded. multilingual-e5 models expect a "query: " prefix.
    """

    def __init__(
        self,
        model_name: str = "intfloat/multilingual-e5-large",
        prefix: str = "query: ",
        batch_size: int = 32,
        encoder: Optional[Callable[[list[str]], np.ndarray]] = None,
    ):
        if encoder is None:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(model_name)

            def encoder(texts: list[str]) -> np.ndarray:
                return model.encode(
                    texts,
                    batch_size=batch_size,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )

        self._encode = encoder
        self.prefix = prefix
        self._cache: dict[str, Optional[np.ndarray]] = {}

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        """Encode raw texts (the prefix is added here) into unit-length float32 vectors."""
        vectors = np.asarray(self._encode([self.prefix + t for t in texts]), dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.clip(norms, 1e-12, None)

    def embed(self, records: Iterable[dict]) -> None:
        """Embed all records not yet in the cache, in one batch."""
        todo = {r["norm_id"]: r for r in records if r["norm_id"] not in self._cache}
        to_encode = []
        for norm_id, record in todo.items():
            text = norm_text(record)
            if text:
                to_encode.append((norm_id, text))
            else:
                self._cache[norm_id] = None  # nothing to compare

        if not to_encode:
            return
        vectors = self.encode_texts([text for _, text in to_encode])
        for (norm_id, _), vector in zip(to_encode, vectors):
            self._cache[norm_id] = vector

    def similarity(self, a: dict, b: dict) -> Optional[float]:
        """Cosine similarity in [-1, 1], or None if either norm has no text."""
        self.embed([a, b])
        va, vb = self._cache[a["norm_id"]], self._cache[b["norm_id"]]
        if va is None or vb is None:
            return None
        return float(np.dot(va, vb))

    def similarities(self, pairs: list[tuple[dict, dict]]) -> list[Optional[float]]:
        """Score many pairs; embeds all unseen norms in one batch first."""
        self.embed(record for pair in pairs for record in pair)
        return [self.similarity(a, b) for a, b in pairs]