# -*- coding: utf-8 -*-
"""
Semantic candidate generation via embeddings (2026-09-23) -- replaces reliance on the
incident-reporting-only keyword family as the way two provisions from different,
uncited instruments become a candidate pair, so C1 (and later C2/C3) can compare
provisions on ANY topic, not just incident/breach reporting.

Built after checking a real gap directly, at zero cost: the AI Act's 10-year
documentation-retention rule (art. 23) and Cbw's 12/60-month data-retention rule
(art. 65) were NEVER a candidate pair under the old keyword-family approach, since
"retention" isn't in that lexicon and the two instruments don't cite each other. That
gap -- not an absence of real contradictions -- is the leading explanation for why the
full-corpus C1 run found nothing: candidate generation was never wide enough to let
most of the corpus be compared at all.

Cheap by design, on purpose, given the team's cost concerns: embeddings cost a small
fraction of a chat-completion call -- text-embedding-3-small is $0.02 per million
tokens, so embedding the whole corpus (~1,000 short paragraphs) costs a fraction of a
cent, not dollars. This is meant to widen the net BEFORE spending anything further on
the (much more expensive) duty_conflict adjudication calls.
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
EMBEDDING_MODEL = "text-embedding-3-small"
CACHE_PATH = DATA / "norm_embeddings_cache.json"


def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")


def compute_embeddings(client, texts: list[str]) -> dict[str, list[float]]:
    """Returns {text: embedding_vector}. Persistently cached on disk, keyed by a hash
    of the text, so re-running this later (e.g. once the corpus grows) never re-pays
    for a paragraph already embedded."""
    cache = _load_cache()
    to_embed, keys = [], []
    for text in texts:
        key = _cache_key(text)
        if key not in cache:
            to_embed.append(text)
            keys.append(key)

    if to_embed:
        BATCH = 500  # comfortably under the API's per-request input-count limit
        for i in range(0, len(to_embed), BATCH):
            batch_texts = to_embed[i:i + BATCH]
            batch_keys = keys[i:i + BATCH]
            resp = client.embeddings.create(input=batch_texts, model=EMBEDDING_MODEL)
            for key, item in zip(batch_keys, resp.data):
                cache[key] = item.embedding
        _save_cache(cache)

    return {text: cache[_cache_key(text)] for text in texts}


def _cosine_similarity_matrix(vectors: list[list[float]]):
    import numpy as np
    arr = np.array(vectors)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    normalized = arr / norms
    return normalized @ normalized.T


def semantic_candidate_pairs(client, texts: list[str], k: int = 8) -> set[tuple[int, int]]:
    """Returns {(i, j), ...} index pairs into `texts` (i < j) where j is among i's
    top-k most similar OTHER texts by embedding cosine similarity, or vice versa (kNN
    isn't symmetric, so both directions are unioned). Top-k rather than a fixed
    similarity-score cutoff on purpose: an absolute cosine threshold is notoriously
    corpus-dependent and would need calibration against a labelled sample we don't
    have yet (Stage 9); bounding by k avoids inventing a number that hasn't been
    checked against anything."""
    import numpy as np

    embeddings = compute_embeddings(client, texts)
    vectors = [embeddings[t] for t in texts]
    sim = _cosine_similarity_matrix(vectors)
    np.fill_diagonal(sim, -1.0)

    pairs = set()
    n = len(texts)
    k = min(k, n - 1) if n > 1 else 0
    for i in range(n):
        top_k_idx = np.argsort(sim[i])[::-1][:k]
        for j in top_k_idx:
            if sim[i][j] <= 0:
                continue
            pairs.add((min(i, int(j)), max(i, int(j))))
    return pairs
