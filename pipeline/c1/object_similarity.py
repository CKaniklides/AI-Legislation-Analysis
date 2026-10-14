"""
Object similarity with character n-gram TF-IDF.

For each norm, the object text (object_text.build_object_text: the action minus verbs,
actor and circumstances) is turned into two feature blocks:

  words       the content words, as character n-grams of 3-5 letters within word
              boundaries. Handles Dutch inflection and compounds without a stemmer
              (certificaat/certificaten, gegevens/persoonsgegevens).
  references  explicit references as whole tokens ("bijlage xi", "artikel 53",
              "2016/679"). Two norms pointing to the same annex or article usually
              concern the same object. Whole tokens, so "bijlage XI" does not partly
              match "bijlage XII".

Both blocks are TF-IDF weighted over the corpus being analysed, so terms that occur
in many norms ("AI-systeem", "hoog risico", "aanbieder") get little weight and rare
terms (logs, mandaat, certificaten) dominate. Nothing is curated: run it on DORA or
NIS2 and the weights adapt to that corpus.

Each block is L2-normalised, the reference block is scaled by REFERENCE_WEIGHT, and
the result is normalised again. Similarity is the cosine between two norms.

Pairs are then put in one of three states:
    same        similarity >= same_threshold
    different   similarity <= different_threshold
    uncertain   in between
    unknown     at least one norm has no usable object
Only "different" changes the verb score (see action_scoring.py).

Requires scikit-learn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from object_text import ObjectText

FLAGS = re.IGNORECASE

NGRAM_RANGE = (3, 5)
REFERENCE_WEIGHT = 0.5

SAME_THRESHOLD = 0.50
DIFFERENT_THRESHOLD = 0.15

# "artikel 53", "artikel 3:4", "bijlage XI", "hoofdstuk III", "afdeling 2", "2016/679"
_REFERENCE = re.compile(
    r"\b(artikel|artikelen|bijlage|bijlagen|hoofdstuk|afdeling|titel)\s+"
    r"([0-9]+(?::[0-9]+)?[a-z]?|[ivxlcdm]+)\b"
    r"|\b(\d{4}/\d{1,4})\b",
    FLAGS,
)
_SINGULAR = {"artikelen": "artikel", "bijlagen": "bijlage"}


def reference_tokens(text: str) -> list[str]:
    out = []
    for m in _REFERENCE.finditer(text):
        if m.group(3):
            out.append(m.group(3))
        else:
            kind = _SINGULAR.get(m.group(1).lower(), m.group(1).lower())
            out.append(f"{kind} {m.group(2).lower()}")
    return out


@dataclass
class ObjectFeatures:
    words: str              # content words joined by spaces (input for char n-grams)
    references: list[str]

    @property
    def usable(self) -> bool:
        return bool(self.words.strip()) or bool(self.references)


def object_features(obj: ObjectText) -> ObjectFeatures:
    return ObjectFeatures(words=" ".join(obj.content_words),
                          references=reference_tokens(obj.text))


def _stem(w: str) -> str:
    return w[:6]


def shared_terms(a: ObjectFeatures, b: ObjectFeatures, limit: int = 6) -> list[str]:
    """Human-readable explanation: shared references, then content words sharing a
    6-letter prefix (certificaten ~ certificaat)."""
    out = [r for r in dict.fromkeys(a.references) if r in set(b.references)]
    b_stems = {_stem(w) for w in b.words.split()}
    seen = set()
    for w in a.words.split():
        s = _stem(w)
        if s in b_stems and s not in seen:
            seen.add(s)
            out.append(w)
    return out[:limit]


class ObjectSimilarity:
    """Fit on the object features of all norms; then look up pair similarities."""

    def __init__(self, features: dict[str, ObjectFeatures]):
        import numpy as np
        from scipy.sparse import hstack
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.preprocessing import normalize

        self.features = features
        self.ids = [nid for nid, f in features.items() if f.usable]
        self.row = {nid: k for k, nid in enumerate(self.ids)}
        self.vocab_words = 0
        self.vocab_refs = 0
        if not self.ids:
            self.S = np.zeros((0, 0))
            return

        words = [features[nid].words for nid in self.ids]
        refs = [features[nid].references for nid in self.ids]

        blocks = []
        if any(w.strip() for w in words):
            vw = TfidfVectorizer(analyzer="char_wb", ngram_range=NGRAM_RANGE,
                                 lowercase=True, sublinear_tf=True)
            blocks.append(vw.fit_transform(words))
            self.vocab_words = len(vw.vocabulary_)
        if any(refs):
            vr = TfidfVectorizer(analyzer=lambda toks: toks, lowercase=False)
            blocks.append(vr.fit_transform(refs) * REFERENCE_WEIGHT)
            self.vocab_refs = len(vr.vocabulary_)

        X = normalize(hstack(blocks).tocsr()) if len(blocks) > 1 else blocks[0]
        self.S = (X @ X.T).toarray()

    def similarity(self, id_a: str, id_b: str) -> float | None:
        ra, rb = self.row.get(id_a), self.row.get(id_b)
        if ra is None or rb is None:
            return None
        return float(self.S[ra, rb])


def object_state(sim: float | None, same_threshold: float = SAME_THRESHOLD,
                 different_threshold: float = DIFFERENT_THRESHOLD) -> str:
    if sim is None:
        return "unknown"
    if sim >= same_threshold:
        return "same"
    if sim <= different_threshold:
        return "different"
    return "uncertain"