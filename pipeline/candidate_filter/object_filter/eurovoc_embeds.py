"""
EuroVoc preparation, Step 4: embed labels and compute reference distributions.

1. Embeds every Dutch EuroVoc label (preferred and alternative) with a
   sentence-transformers model (default: intfloat/multilingual-e5-base with
   the symmetric-task prefix "query: ").
2. Optionally embeds the objects from your norm records with the SAME model
   and prefix, and computes two reference distributions:
     - top-1: each object's highest cosine to any label. Set the Step 7
       fallback threshold as a percentile of THIS distribution: the
       percentile directly tells you what share of objects passes.
     - random pairs: cosine between random object-label pairs. A baseline
       that shows the model's anisotropy and gives a mean/std for z-scores.
   (A threshold on the random-pair percentiles would not work: the best of
   ~17,000 labels always lies in the far tail of the random distribution.)
3. Writes each object's nearest labels, to choose the threshold by eye.

With --units (the norm_object_units.jsonl from Step 6), the cleaned object
units are embedded instead of the raw objects. The reference distribution then
describes what Step 7 actually maps, so rerun this step after every rerun of
Step 6: the fallback threshold shifts with the cleaning.

The model and prefix are written to embedding_config.json. Any later code that
embeds objects must use them; the simplest way is to import embed_texts and
load_config from this file:

    from eurovoc_embed import embed_texts, load_config
    cfg = load_config(Path("data/eurovoc"))
    vecs = embed_texts(texts, cache_path, cfg["model"], cfg["prefix"])

Labels and objects are embedded as natural text (original casing, label
qualifiers kept), not as the lemmatized form from Step 2: the lemmatized form
is for exact matching, while the encoder was trained on natural text.

Outputs (in --dir)
  embedding_config.json        model and prefix used (read this downstream)
  emb_labels.npz               cache + label vectors (key, texts, vectors)
  label_index.csv              concept, label, label_type (label = text key)
  emb_objects.npz              cache + object vectors            (with --records)
  reference_distribution.json  percentiles of top-1, margin, random pairs (idem)
  object_nearest_labels.csv    per object: top-k labels, cosines, z-scores (idem)

Usage
  python eurovoc_embed.py                          # labels only
  python eurovoc_embed.py --records data/norms.json
  python eurovoc_embed.py --units data/conflict/eurovoc/norm_object_units.jsonl
  python eurovoc_embed.py --model intfloat/multilingual-e5-small

Requirements
  pip install sentence-transformers
  (the model is downloaded from Hugging Face on first use, ~1 GB for base)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("eurovoc.embed")

DEFAULT_MODEL = "intfloat/multilingual-e5-base"
DEFAULT_PREFIX = "query: "
CONFIG_FILE = "embedding_config.json"

PERCENTILES = [1, 5, 10, 20, 30, 40, 50, 60, 70, 80, 85, 90, 95, 99]


# --------------------------------------------------------------------------
# Embedding with cache (same pattern as embed_norms)
# --------------------------------------------------------------------------

def _save_cache(path: Path, key: str, cached: dict[str, np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    texts = list(cached)
    with open(tmp, "wb") as fh:
        np.savez(fh, key=np.array(key), texts=np.array(texts),
                 vectors=np.stack([cached[t] for t in texts]))
    os.replace(tmp, path)


class Encoder:
    """Thin wrapper around sentence-transformers. Loaded only when needed."""

    def __init__(self, model_name: str, prefix: str, batch_size: int = 64):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(model_name)
        self.prefix = prefix
        self.batch_size = batch_size

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        return self.model.encode([self.prefix + t for t in texts],
                                 batch_size=self.batch_size,
                                 normalize_embeddings=True,
                                 show_progress_bar=False,
                                 convert_to_numpy=True)


def save_config(d: Path, model_name: str, prefix: str) -> None:
    with open(d / CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"model": model_name, "prefix": prefix}, f, indent=2)


def load_config(d: Path) -> dict:
    with open(d / CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def _unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    return v / np.where(norms == 0, 1.0, norms)


def embed_texts(texts: list[str], cache_path: Path,
                model_name: str = DEFAULT_MODEL, prefix: str = DEFAULT_PREFIX,
                chunk: int = 1000) -> dict[str, np.ndarray]:
    """Return {text: unit vector} for all non-empty texts. Only texts missing
    from the cache are encoded, in chunks, saving the cache after each chunk;
    the model is not loaded when everything is cached."""
    cache_key = f"{model_name}|{prefix}"
    cached: dict[str, np.ndarray] = {}
    if cache_path.is_file():
        with np.load(cache_path, allow_pickle=False) as z:
            if str(z["key"]) == cache_key:
                cached = {str(t): v for t, v in zip(z["texts"], z["vectors"])}
            else:
                log.info("Cache %s was built with another model; re-encoding.",
                         cache_path.name)

    missing = sorted({t for t in texts if t and t not in cached})
    if missing:
        log.info("Encoding %d text(s) into %s with %s ...",
                 len(missing), cache_path.name, model_name)
        encoder = Encoder(model_name, prefix)
        for i in range(0, len(missing), chunk):
            batch = missing[i:i + chunk]
            vecs = _unit(encoder.encode_texts(batch))
            cached.update(zip(batch, vecs))
            _save_cache(cache_path, cache_key, cached)
            log.info("  %d / %d", min(i + chunk, len(missing)), len(missing))
    else:
        log.info("All %d text(s) for %s found in cache.",
                 len({t for t in texts if t}), cache_path.name)
    return cached


def as_matrix(texts: list[str], vectors: dict[str, np.ndarray]) -> np.ndarray:
    return np.stack([vectors[t] for t in texts]).astype(np.float32)


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def load_label_texts(d: Path) -> pd.DataFrame:
    labels = pd.read_csv(d / "labels_normalized.csv", dtype=str)
    labels["label"] = labels["label"].str.strip()
    labels = labels[labels["label"].fillna("") != ""]
    labels = labels[["concept", "label", "label_type"]].drop_duplicates()
    labels.to_csv(d / "label_index.csv", index=False)
    return labels


def _get_path(obj, path: list[str]):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def load_object_texts(records_path: Path, field: str) -> list[str]:
    """Reads a JSON list or JSONL file of norm records; field is a dotted path
    such as 'norm.object'. Returns unique non-empty object texts."""
    raw = records_path.read_text(encoding="utf-8").strip()
    if raw.startswith("["):
        records = json.loads(raw)
    else:
        records = [json.loads(line) for line in raw.splitlines() if line.strip()]
    path = field.split(".")
    texts, n_null = [], 0
    for r in records:
        v = _get_path(r, path)
        if isinstance(v, str) and v.strip():
            texts.append(" ".join(v.split()))
        else:
            n_null += 1
    log.info("Records: %d, with object: %d, without: %d",
             len(records), len(texts), n_null)
    return sorted(set(texts))


def load_unit_texts(units_path: Path) -> list[str]:
    """Unique unit texts from Step 6 (norm_object_units.jsonl)."""
    with open(units_path, encoding="utf-8") as f:
        texts = {u for line in f if line.strip() for u in json.loads(line)["units"] if u}
    log.info("Unique units: %d", len(texts))
    return sorted(texts)


# --------------------------------------------------------------------------
# Reference distributions
# --------------------------------------------------------------------------

def nearest_labels(O: np.ndarray, L: np.ndarray, k: int, chunk: int = 512
                   ) -> tuple[np.ndarray, np.ndarray]:
    """Top-k label indices and cosines per object, computed in chunks."""
    idx = np.zeros((len(O), k), dtype=np.int64)
    sim = np.zeros((len(O), k), dtype=np.float32)
    for i in range(0, len(O), chunk):
        s = O[i:i + chunk] @ L.T
        part = np.argpartition(-s, kth=k - 1, axis=1)[:, :k]
        part_s = np.take_along_axis(s, part, axis=1)
        order = np.argsort(-part_s, axis=1)
        idx[i:i + chunk] = np.take_along_axis(part, order, axis=1)
        sim[i:i + chunk] = np.take_along_axis(part_s, order, axis=1)
    return idx, sim


def random_pair_cosines(O: np.ndarray, L: np.ndarray, n: int, seed: int
                        ) -> np.ndarray:
    rng = np.random.default_rng(seed)
    oi = rng.integers(0, len(O), n)
    li = rng.integers(0, len(L), n)
    return np.einsum("ij,ij->i", O[oi], L[li])


def describe(x: np.ndarray) -> dict:
    return {"mean": float(np.mean(x)), "std": float(np.std(x)),
            "min": float(np.min(x)), "max": float(np.max(x)),
            "percentiles": {str(p): float(np.percentile(x, p)) for p in PERCENTILES}}


# --------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", type=Path, default=Path("data/conflict/eurovoc"))
    p.add_argument("--records", type=Path, default=None,
                   help="norm records (JSON list or JSONL); omit to embed labels only")
    p.add_argument("--units", type=Path, default=None,
                   help="Step 6 norm_object_units.jsonl; embeds cleaned units "
                        "instead of raw objects (takes precedence over --records)")
    p.add_argument("--object-field", default="norm.object",
                   help="dotted path to the object text in a record")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--prefix", default=DEFAULT_PREFIX)
    p.add_argument("--top-k", type=int, default=3)
    p.add_argument("--random-pairs", type=int, default=200_000)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--chunk", type=int, default=1000, help="texts per encoding chunk")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    # Config: fail loudly if a different model was used before, rather than
    # mixing vector spaces between labels and objects.
    cfg_path = args.dir / CONFIG_FILE
    if cfg_path.exists():
        old = load_config(args.dir)
        if (old["model"], old["prefix"]) != (args.model, args.prefix):
            log.warning("Model/prefix changed from %s / %r to %s / %r. Label and "
                        "object vectors will be re-encoded; anything downstream "
                        "must use the new values.", old["model"], old["prefix"],
                        args.model, args.prefix)
    save_config(args.dir, args.model, args.prefix)

    # Labels
    labels = load_label_texts(args.dir)
    label_texts = sorted(set(labels["label"]))
    label_vecs = embed_texts(label_texts, args.dir / "emb_labels.npz",
                             args.model, args.prefix, args.chunk)
    L = as_matrix(label_texts, label_vecs)
    log.info("Label vectors: %s", L.shape)

    if args.records is None and args.units is None:
        log.info("No --records or --units given: labels embedded, distributions skipped.")
        return

    # Objects (cleaned units from Step 6, or raw objects)
    if args.units is not None:
        object_texts = load_unit_texts(args.units)
        source = f"units:{args.units.name}"
    else:
        object_texts = load_object_texts(args.records, args.object_field)
        source = f"records:{args.object_field}"
    if not object_texts:
        raise SystemExit("No object texts found; check --units / --records / --object-field.")
    object_vecs = embed_texts(object_texts, args.dir / "emb_objects.npz",
                              args.model, args.prefix, args.chunk)
    O = as_matrix(object_texts, object_vecs)
    log.info("Object vectors: %s", O.shape)

    # Distributions
    k = min(args.top_k, len(label_texts))
    idx, sim = nearest_labels(O, L, k)
    rand = random_pair_cosines(O, L, args.random_pairs, args.seed)
    mu, sd = float(rand.mean()), float(rand.std()) or 1.0
    ref = {
        "model": args.model, "prefix": args.prefix,
        "object_field": args.object_field, "object_source": source,
        "n_label_texts": len(label_texts), "n_object_texts": len(object_texts),
        "top1": describe(sim[:, 0]),
        "random_pairs": describe(rand),
        "note": ("Set the fallback threshold as a percentile of top1: "
                 "percentile p means (100 - p)% of objects pass. Random pairs are "
                 "a baseline; z = (cos - random mean) / random std."),
    }
    if k > 1:
        ref["top1_margin"] = describe(sim[:, 0] - sim[:, 1])
    with open(args.dir / "reference_distribution.json", "w", encoding="utf-8") as f:
        json.dump(ref, f, ensure_ascii=False, indent=2)

    # Per-object nearest labels, sorted by top-1 score for easy inspection
    label_to_concepts = labels.groupby("label")["concept"].apply(
        lambda s: "|".join(sorted(set(s)))).to_dict()
    rows = {"object": object_texts}
    for j in range(k):
        rows[f"label_{j+1}"] = [label_texts[i] for i in idx[:, j]]
        rows[f"cos_{j+1}"] = np.round(sim[:, j], 4)
        rows[f"z_{j+1}"] = np.round((sim[:, j] - mu) / sd, 2)
        rows[f"concepts_{j+1}"] = [label_to_concepts[label_texts[i]] for i in idx[:, j]]
    nn = pd.DataFrame(rows).sort_values("cos_1", ascending=False)
    nn["top1_percentile"] = np.round(nn["cos_1"].rank(pct=True) * 100, 1)
    nn.to_csv(args.dir / "object_nearest_labels.csv", index=False, encoding="utf-8-sig")

    t = ref["top1"]["percentiles"]
    log.info("Random pairs: mean %.3f, std %.3f, p99 %.3f",
             mu, sd, ref["random_pairs"]["percentiles"]["99"])
    log.info("Top-1 cosine: p10 %.3f, p20 %.3f, p50 %.3f, p80 %.3f",
             t["10"], t["20"], t["50"], t["80"])
    log.info("Inspect object_nearest_labels.csv to see where matches turn bad.")


if __name__ == "__main__":
    main()