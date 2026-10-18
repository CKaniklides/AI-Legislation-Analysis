"""
EuroVoc Step 4: map each norm's object to EuroVoc concepts.

Four-level cascade; the first level that yields a concept wins:
  1. phrase    the normalized object equals a normalized label
  2. head      the head noun's lemma equals a normalized label
  3. compound  the longest proper ending of the head noun that is a label,
               at least --min-tail-len characters (omgevingsvergunning -> vergunning)
  4. embedding nearest labels by cosine, accepted only above the fallback
               percentile of the Step 4 top-1 reference distribution

Up to K concepts are kept per object, each with a confidence:
  - levels 1-3: the level's fixed confidence. A label that points to several
    concepts is ambiguous: candidates are ordered by embedding similarity to
    the object, and their confidence is multiplied by --ambiguity-factor.
  - level 4: scales linearly with the candidate's percentile in the top-1
    reference distribution, from --conf-embedding-min at the threshold to
    --conf-embedding-max at the 100th percentile.
Objects that pass no level are unmapped (score 0 downstream).

With --units (Step 6 output, norm_object_units.jsonl) the cleaned object units
are mapped instead of the raw objects. A norm then has zero, one or several
units (several for a split coordination); each is mapped separately. Without
--units, each raw object is its own single unit.

All parameters are CLI options and are written to mapping_config.json next to
the results. The defaults are placeholders to be calibrated in Step 12.

Inputs (from Steps 2 and 4, in --dir)
  label_lookup.json            normalized label -> concepts
  label_index.csv              label text -> concept
  emb_labels.npz               label vectors
  embedding_config.json        model and prefix
  reference_distribution.json  top-1 percentiles
plus either --units (Step 6) or your raw norm records (--records).

Outputs (in --dir)
  object_mapping.jsonl         one line per unique unit: head, level, candidates
  norm_object_mapping.jsonl    one line per norm: norm_id, object, units (each
                               with level and candidates), plus level and
                               candidates merged over the units (top K)
  object_mapping.csv           one row per unit x candidate, for inspection
  mapping_config.json          parameters used

Usage
  python eurovoc_map.py --units data/conflict/eurovoc/norm_object_units.jsonl
  python eurovoc_map.py --records <norm records>      # raw objects, no Step 6
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from eurovoc_embeds import as_matrix, embed_texts, load_config
from eurovoc_prepare import load_nlp, normalize_texts

log = logging.getLogger("eurovoc.map")

LEVELS = ["phrase", "head", "compound", "embedding"]


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def _get_path(obj, path: list[str]):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def load_records(path: Path, object_field: str, id_field: str) -> list[dict]:
    raw = path.read_text(encoding="utf-8").strip()
    records = json.loads(raw) if raw.startswith("[") else \
        [json.loads(l) for l in raw.splitlines() if l.strip()]
    out = []
    for r in records:
        v = _get_path(r, object_field.split("."))
        text = " ".join(v.split()) if isinstance(v, str) else ""
        out.append({"norm_id": _get_path(r, id_field.split(".")), "object": text,
                    "units": [text] if text else []})
    return out


def load_units(path: Path) -> list[dict]:
    """Per-norm units from Step 6 (norm_object_units.jsonl)."""
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def norm_result(rec: dict, results: dict, k: int) -> dict:
    """Per-unit mapping of a norm, plus candidates merged over its units."""
    units, merged = [], {}
    for text in rec["units"]:
        r = results.get(text)
        cands = r["candidates"] if r else []
        for c in cands:
            if c["concept"] not in merged or c["confidence"] > merged[c["concept"]]["confidence"]:
                merged[c["concept"]] = dict(c, unit=text)
        units.append({"text": text, "level": r["level"] if r else None, "candidates": cands})
    levels = [u["level"] for u in units if u["level"]]
    return {"norm_id": rec["norm_id"], "object": rec["object"],
            "level": min(levels, key=LEVELS.index) if levels else None,
            "candidates": sorted(merged.values(), key=lambda c: -c["confidence"])[:k],
            "units": units}


class LabelSpace:
    """Label vectors, label -> concepts, and normalized label lookup."""

    def __init__(self, d: Path):
        with open(d / "label_lookup.json", encoding="utf-8") as f:
            self.lookup: dict[str, list[str]] = json.load(f)
        index = pd.read_csv(d / "label_index.csv", dtype=str)
        self.label_concepts = index.groupby("label")["concept"].apply(
            lambda s: sorted(set(s))).to_dict()
        cfg = load_config(d)
        self.model, self.prefix = cfg["model"], cfg["prefix"]
        self.texts = sorted(self.label_concepts)
        vecs = embed_texts(self.texts, d / "emb_labels.npz", self.model, self.prefix)
        self.L = as_matrix(self.texts, vecs)
        # concept -> row indices of its labels (for lexical tie-breaking)
        self.concept_rows: dict[str, list[int]] = {}
        for i, t in enumerate(self.texts):
            for c in self.label_concepts[t]:
                self.concept_rows.setdefault(c, []).append(i)
        # one display label per concept: the preferred label where available
        pref = index[index["label_type"] == "pref"].drop_duplicates("concept")
        self.concept_label = dict(zip(pref["concept"], pref["label"]))
        for c, rows in self.concept_rows.items():
            self.concept_label.setdefault(c, self.texts[rows[0]])


class Reference:
    """Top-1 percentile table from Step 4, with interpolation both ways."""

    def __init__(self, d: Path):
        with open(d / "reference_distribution.json", encoding="utf-8") as f:
            pct = json.load(f)["top1"]["percentiles"]
        pairs = sorted((float(p), float(c)) for p, c in pct.items())
        self.ps = np.array([p for p, _ in pairs])
        self.cs = np.maximum.accumulate(np.array([c for _, c in pairs]))

    def cosine_at(self, percentile: float) -> float:
        return float(np.interp(percentile, self.ps, self.cs))

    def percentile_of(self, cosine: float) -> float:
        # beyond the table, extrapolate to 0 / 100 rather than clipping at 1 / 99
        ps = np.concatenate([[0.0], self.ps, [100.0]])
        cs = np.concatenate([[self.cs[0] - 1e-6], self.cs, [max(self.cs[-1], 1.0)]])
        return float(np.interp(cosine, cs, ps))


# --------------------------------------------------------------------------
# Linguistic analysis
# --------------------------------------------------------------------------

def head_lemma(doc) -> str | None:
    """Head noun of the object: the syntactic root if it is a noun, otherwise
    the noun closest to the root. None if the object contains no noun
    (e.g. 'het volgende')."""
    nouns = [t for t in doc if t.pos_ in ("NOUN", "PROPN")]
    if not nouns:
        return None
    roots = [t for t in doc if t.dep_ == "ROOT"]
    if roots and roots[0].pos_ in ("NOUN", "PROPN"):
        return roots[0].lemma_.lower()

    def depth(t):
        d = 0
        while t.head is not t and d < 50:
            t, d = t.head, d + 1
        return d
    return min(nouns, key=lambda t: (depth(t), t.i)).lemma_.lower()


def compound_tail(head: str, lookup: dict, min_len: int) -> str | None:
    """Longest proper ending of head (at least min_len chars) that is a label."""
    for i in range(1, len(head) - min_len + 1):
        tail = head[i:]
        if tail in lookup:
            return tail
    return None


# --------------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------------

def lexical_candidates(concepts: list[str], obj_vec: np.ndarray, space: LabelSpace,
                       conf: float, args) -> list[dict]:
    """Order the concepts of one lexical match by embedding similarity to the
    object, keep the top K, and lower confidence when the label is ambiguous."""
    scored = []
    for c in concepts:
        rows = space.concept_rows.get(c)
        cos = float(np.max(space.L[rows] @ obj_vec)) if rows else float("nan")
        scored.append((c, cos))
    scored.sort(key=lambda x: -np.nan_to_num(x[1], nan=-1.0))
    factor = 1.0 if len(concepts) == 1 else args.ambiguity_factor
    return [{"concept": c, "label": space.concept_label.get(c, ""),
             "confidence": round(conf * factor, 4),
             "cosine": None if np.isnan(cos) else round(cos, 4)}
            for c, cos in scored[:args.k]]


def embedding_candidates(obj_vec: np.ndarray, space: LabelSpace, ref: Reference,
                         threshold_cos: float, args) -> list[dict]:
    sims = space.L @ obj_vec
    n = min(len(sims), max(args.k * 10, 30))
    top = np.argpartition(-sims, n - 1)[:n]
    top = top[np.argsort(-sims[top])]
    out, seen = [], set()
    for i in top:
        cos = float(sims[i])
        if cos < threshold_cos:
            break
        for c in space.label_concepts[space.texts[i]]:
            if c in seen:
                continue
            seen.add(c)
            pct = ref.percentile_of(cos)
            span = max(100.0 - args.fallback_percentile, 1e-9)
            frac = min(max((pct - args.fallback_percentile) / span, 0.0), 1.0)
            conf = args.conf_embedding_min + frac * (args.conf_embedding_max
                                                     - args.conf_embedding_min)
            out.append({"concept": c, "label": space.texts[i],
                        "confidence": round(conf, 4), "cosine": round(cos, 4),
                        "percentile": round(pct, 1)})
            if len(out) == args.k:
                return out
    return out


def map_objects(objects: list[str], space: LabelSpace, ref: Reference, args) -> dict:
    nlp_norm = load_nlp(args.spacy_model)            # same pipeline as Step 2
    import spacy
    nlp_parse = spacy.load(args.spacy_model, disable=["ner"])

    normalized = normalize_texts(objects, nlp_norm)
    heads = [head_lemma(doc) for doc in nlp_parse.pipe(objects, batch_size=256)]
    obj_vecs = embed_texts(objects, args.dir / "emb_objects.npz", space.model, space.prefix)
    threshold_cos = ref.cosine_at(args.fallback_percentile)
    log.info("Embedding fallback: percentile %.1f -> cosine %.4f",
             args.fallback_percentile, threshold_cos)

    results = {}
    for obj, norm, head in zip(objects, normalized, heads):
        v = obj_vecs[obj]
        level, match, cands = None, None, []
        if norm in space.lookup:
            level, match = "phrase", norm
            cands = lexical_candidates(space.lookup[norm], v, space, args.conf_phrase, args)
        elif head and head in space.lookup:
            level, match = "head", head
            cands = lexical_candidates(space.lookup[head], v, space, args.conf_head, args)
        elif head and (tail := compound_tail(head, space.lookup, args.min_tail_len)):
            level, match = "compound", tail
            cands = lexical_candidates(space.lookup[tail], v, space, args.conf_compound, args)
        else:
            cands = embedding_candidates(v, space, ref, threshold_cos, args)
            if cands:
                level = "embedding"
        results[obj] = {"object": obj, "normalized": norm, "head": head,
                        "level": level, "matched_label": match, "candidates": cands}
    return results


# --------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", type=Path, default=Path("data/conflict/object/eurovoc"))
    p.add_argument("--units", type=Path, default=None,
                   help="Step 6 norm_object_units.jsonl (recommended)")
    p.add_argument("--records", type=Path, default=None,
                   help="raw norm records; used only without --units")
    p.add_argument("--object-field", default="norm.object")
    p.add_argument("--id-field", default="norm_id")
    p.add_argument("--spacy-model", default="nl_core_news_sm",
                   help="must be the model used in Step 2")
    p.add_argument("--k", type=int, default=3, help="max candidate concepts per object")
    p.add_argument("--conf-phrase", type=float, default=1.0)
    p.add_argument("--conf-head", type=float, default=0.8)
    p.add_argument("--conf-compound", type=float, default=0.6)
    p.add_argument("--conf-embedding-min", type=float, default=0.2)
    p.add_argument("--conf-embedding-max", type=float, default=0.5)
    p.add_argument("--ambiguity-factor", type=float, default=0.8,
                   help="confidence multiplier when a label points to several concepts")
    p.add_argument("--min-tail-len", type=int, default=5)
    p.add_argument("--fallback-percentile", type=float, default=20.0,
                   help="accept embedding matches at or above this percentile of the "
                        "Step 4 top-1 distribution")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    space = LabelSpace(args.dir)
    ref = Reference(args.dir)
    if args.units is not None:
        records = load_units(args.units)
    elif args.records is not None:
        records = load_records(args.records, args.object_field, args.id_field)
    else:
        raise SystemExit("Give --units (Step 6 output) or --records.")
    objects = sorted({u for r in records for u in r["units"] if u})
    log.info("Norms: %d, unique units to map: %d", len(records), len(objects))

    results = map_objects(objects, space, ref, args)

    with open(args.dir / "object_mapping.jsonl", "w", encoding="utf-8") as f:
        for r in results.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    norm_results = [norm_result(rec, results, args.k) for rec in records]
    with open(args.dir / "norm_object_mapping.jsonl", "w", encoding="utf-8") as f:
        for nr in norm_results:
            f.write(json.dumps(nr, ensure_ascii=False) + "\n")
    cfg = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()}
    cfg["fallback_cosine"] = ref.cosine_at(args.fallback_percentile)
    with open(args.dir / "mapping_config.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

    # Coverage report, over unique objects and over norms
    lv = Counter(r["level"] for r in results.values())
    n = len(objects)
    log.info("Unique units by level (%d):", n)
    for name in LEVELS + [None]:
        log.info("  %-10s %5d  (%.1f%%)", name or "unmapped", lv[name], 100 * lv[name] / max(n, 1))
    with_obj = sum(1 for r in records if r["object"])
    with_units = sum(1 for r in records if r["units"])
    mapped = sum(1 for nr in norm_results if nr["level"])
    log.info("Norms: %d, with object: %d, with units: %d, mapped: %d "
             "(%.1f%% of norms with object)", len(records), with_obj, with_units,
             mapped, 100 * mapped / max(with_obj, 1))
    multi = sum(1 for r in records if len(r["units"]) > 1)
    log.info("Norms with several units: %d", multi)
    amb = sum(1 for r in results.values() if r["level"] in LEVELS[:3]
              and len(r["candidates"]) > 1)
    log.info("Lexical matches with an ambiguous label: %d", amb)


if __name__ == "__main__":
    main()