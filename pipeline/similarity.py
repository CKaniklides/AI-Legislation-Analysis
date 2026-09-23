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


def semantic_candidate_pairs(client, texts: list[str], k: int = 8,
                              groups: "list[str] | None" = None,
                              k_within: "int | None" = None) -> set[tuple[int, int]]:
    """Returns {(i, j), ...} index pairs into `texts` (i < j) where j is among i's
    top-k most similar OTHER texts by embedding cosine similarity, or vice versa (kNN
    isn't symmetric, so both directions are unioned). Top-k rather than a fixed
    similarity-score cutoff on purpose: an absolute cosine threshold is notoriously
    corpus-dependent and would need calibration against a labelled sample we don't
    have yet (Stage 9); bounding by k avoids inventing a number that hasn't been
    checked against anything.

    `groups` (2026-09-24, item 9) -- one label per text (e.g. instrument_id) -- splits
    each node's top-k into two SEPARATE budgets, k_within (same group) and k (cross
    group), rather than one shared top-k. Checked directly why this matters: without
    grouping, candidate generation excluded ALL within-instrument semantic pairs
    entirely (a separate, even harder restriction than what this fixes) on the
    assumption that citation adjacency covers same-instrument relatedness well enough --
    but articles genuinely don't always cite every related provision in the same law
    (e.g. two amendments added years apart covering similar ground). Once within-
    instrument pairs ARE allowed, they need their OWN budget: same-instrument text is
    typically much closer in embedding space (shared drafting boilerplate, defined
    terms, structure) than a genuinely useful cross-instrument match, so a single
    shared top-k would let within-instrument neighbours crowd cross-instrument ones out
    of the same budget entirely."""
    import numpy as np

    embeddings = compute_embeddings(client, texts)
    vectors = [embeddings[t] for t in texts]
    sim = _cosine_similarity_matrix(vectors)
    np.fill_diagonal(sim, -1.0)

    n = len(texts)
    pairs = set()

    if groups is None:
        k = min(k, n - 1) if n > 1 else 0
        for i in range(n):
            top_k_idx = np.argsort(sim[i])[::-1][:k]
            for j in top_k_idx:
                if sim[i][j] <= 0:
                    continue
                pairs.add((min(i, int(j)), max(i, int(j))))
        return pairs

    k_within = k if k_within is None else k_within
    groups_arr = np.array(groups)
    for i in range(n):
        same_group = groups_arr == groups_arr[i]
        same_group[i] = False  # never match a node to itself
        for mask, budget in ((same_group, k_within), (~same_group, k)):
            idxs = np.where(mask)[0]
            if len(idxs) == 0 or budget <= 0:
                continue
            local_sim = sim[i][idxs]
            top = idxs[np.argsort(local_sim)[::-1][:min(budget, len(idxs))]]
            for j in top:
                if sim[i][j] <= 0:
                    continue
                pairs.add((min(i, int(j)), max(i, int(j))))
    return pairs
