"""
EuroVoc preparation, Step 3: embed labels and units, and compute the top-1
reference distribution.

1. Embeds every Dutch EuroVoc label (preferred and alternative) with a
   sentence-transformers model (default: intfloat/multilingual-e5-base with
   the symmetric-task prefix "query: ").
2. Embeds the cleaned object units from Step 2 (norm_object_units.jsonl) with
   the SAME model and prefix.
3. Computes each unit's highest cosine to any label (top-1). The Step 7
   fallback threshold is a percentile of this distribution: percentile p means
   (100 - p)% of units pass. Rerun this step after every rerun of Step 2, since
   the distribution shifts with the cleaning.

The model and prefix are written to embedding_config.json. Any later code that
embeds texts must use them; the simplest way is to import embed_texts and
load_config from this file:

    from eurovoc_embeds import embed_texts, load_config
    cfg = load_config(Path("data/eurovoc"))
    vecs = embed_texts(texts, cache_path, cfg["model"], cfg["prefix"])

Labels and units are embedded as natural text (original casing, label
qualifiers kept), not as the lemmatized form from Step 2: the lemmatized form
is for exact matching, while the encoder was trained on natural text.

Outputs (in --dir)
  embedding_config.json        model and prefix used (read this downstream)
  emb_labels.npz               cache + label vectors (key, texts, vectors)
  label_index.csv              concept, label, label_type (label = text key)
  emb_objects.npz              cache + unit vectors
  reference_distribution.json  percentiles of the units' top-1 cosines

Usage
  python eurovoc_embeds.py --units data/conflict/eurovoc/norm_object_units.jsonl
  python eurovoc_embeds.py --units ... --model intfloat/multilingual-e5-large

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
        """Unit vectors (normalize_embeddings=True), as float32."""
        return self.model.encode([self.prefix + t for t in texts],
                                 batch_size=self.batch_size,
                                 normalize_embeddings=True,
                                 show_progress_bar=False,
                                 convert_to_numpy=True).astype(np.float32)


def save_config(d: Path, model_name: str, prefix: str) -> None:
    with open(d / CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"model": model_name, "prefix": prefix}, f, indent=2)


def load_config(d: Path) -> dict:
    with open(d / CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


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
            cached.update(zip(batch, encoder.encode_texts(batch)))
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


def load_unit_texts(units_path: Path) -> list[str]:
    """Unique unit texts from Step 6 (norm_object_units.jsonl)."""
    with open(units_path, encoding="utf-8") as f:
        texts = {u for line in f if line.strip() for u in json.loads(line)["units"] if u}
    log.info("Unique units: %d", len(texts))
    return sorted(texts)


# --------------------------------------------------------------------------
# Reference distribution
# --------------------------------------------------------------------------

def top1_cosines(O: np.ndarray, L: np.ndarray, chunk: int = 512) -> np.ndarray:
    """Each unit's highest cosine to any label, computed in chunks."""
    return np.concatenate([(O[i:i + chunk] @ L.T).max(axis=1)
                           for i in range(0, len(O), chunk)])


def describe(x: np.ndarray) -> dict:
    return {"mean": float(np.mean(x)), "std": float(np.std(x)),
            "min": float(np.min(x)), "max": float(np.max(x)),
            "percentiles": {str(p): float(np.percentile(x, p)) for p in PERCENTILES}}


# --------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", type=Path, default=Path("data/conflict/object/eurovoc"))
    p.add_argument("--units", type=Path, required=True,
                   help="Step 6 norm_object_units.jsonl")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--prefix", default=DEFAULT_PREFIX)
    p.add_argument("--chunk", type=int, default=1000, help="texts per encoding chunk")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    # Config: warn if a different model was used before, rather than silently
    # mixing vector spaces between labels and units.
    cfg_path = args.dir / CONFIG_FILE
    if cfg_path.exists():
        old = load_config(args.dir)
        if (old["model"], old["prefix"]) != (args.model, args.prefix):
            log.warning("Model/prefix changed from %s / %r to %s / %r. Label and "
                        "unit vectors will be re-encoded; anything downstream "
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

    # Units
    unit_texts = load_unit_texts(args.units)
    if not unit_texts:
        raise SystemExit(f"No units found in {args.units}.")
    unit_vecs = embed_texts(unit_texts, args.dir / "emb_objects.npz",
                            args.model, args.prefix, args.chunk)
    O = as_matrix(unit_texts, unit_vecs)
    log.info("Unit vectors: %s", O.shape)

    # Top-1 reference distribution
    top1 = describe(top1_cosines(O, L))
    ref = {
        "model": args.model, "prefix": args.prefix,
        "object_source": f"units:{args.units.name}",
        "n_label_texts": len(label_texts), "n_object_texts": len(unit_texts),
        "top1": top1,
        "note": ("Set the fallback threshold as a percentile of top1: "
                 "percentile p means (100 - p)% of units pass."),
    }
    with open(args.dir / "reference_distribution.json", "w", encoding="utf-8") as f:
        json.dump(ref, f, ensure_ascii=False, indent=2)

    t = top1["percentiles"]
    log.info("Top-1 cosine: p20 %.3f, p50 %.3f, p70 %.3f, p80 %.3f",
             t["20"], t["50"], t["70"], t["80"])


if __name__ == "__main__":
    main()