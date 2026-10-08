"""Cosine similarity for every pair in a small subset of norms (e.g. to calibrate
SIMILARITY_FLOOR in stageA_filtering.py).

Input : a JSON array of norm records (same format as merged_norms.json)
Output: a CSV with one row per pair, highest similarity first, plus the top pairs printed

Which norms are compared:
  --ids ID [ID ...]   these norm_ids
  --ids-file FILE     norm_ids listed one per line
  --limit N           the first N norms of the file (also caps --ids / --ids-file)
  (nothing)           every norm in the file, refused above MAX_NORMS

Usage
-----
    python similarity_subset.py --input my_norms.json
    python similarity_subset.py --input merged_norms.json --ids "32016R0679:17:..." "32024R1689:10:..."
    python similarity_subset.py --input merged_norms.json --limit 50 --out sims.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from itertools import combinations
from pathlib import Path
from typing import Optional

from norm_similarity import NormSimilarity, norm_text

MAX_NORMS = 300            # 300 norms = 44,850 pairs; above this, use stageA_filtering.py
MODEL_NAME = "intfloat/multilingual-e5-large"
MODEL_PREFIX = "query: "   # e5 models expect this prefix; use "" for other models


def subset_similarities(records: list[dict], sim: Optional[NormSimilarity] = None) -> list[dict]:
    """Score every pair of ``records`` and return rows sorted by similarity, highest first.
    Pairs where either norm has no action/trigger text get similarity None and go last."""
    sim = sim or NormSimilarity(model_name=MODEL_NAME, prefix=MODEL_PREFIX)
    pairs = list(combinations(records, 2))
    scores = sim.similarities(pairs)

    rows = []
    for (a, b), score in zip(pairs, scores):
        ha, hb = a.get("hierarchy") or {}, b.get("hierarchy") or {}
        rows.append({
            "norm_id_a": a["norm_id"],
            "norm_id_b": b["norm_id"],
            "similarity": None if score is None else round(score, 4),
            "same_article": (ha.get("instrument_id"), ha.get("article"))
                            == (hb.get("instrument_id"), hb.get("article")),
            "deontic_a": (a.get("norm") or {}).get("deontic"),
            "deontic_b": (b.get("norm") or {}).get("deontic"),
            "text_a": norm_text(a),
            "text_b": norm_text(b),
        })
    rows.sort(key=lambda r: (r["similarity"] is None, -(r["similarity"] or 0.0)))
    return rows


def select_records(records: list[dict], ids: Optional[list[str]], limit: Optional[int]) -> list[dict]:
    """Pick the subset: the given norm_ids (in the order given), else the whole file;
    then cut to ``limit``. Raises ValueError on unknown ids."""
    if ids:
        by_id = {r["norm_id"]: r for r in records}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise ValueError(f"{len(missing)} norm_id(s) not in the input, e.g. {missing[:3]}")
        records = [by_id[i] for i in dict.fromkeys(ids)]   # keep order, drop repeats
    return records[:limit] if limit else records


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--input", type=Path, required=True, help="JSON array of norm records")
    ap.add_argument("--ids", nargs="+", help="norm_ids to compare")
    ap.add_argument("--ids-file", type=Path, help="file with one norm_id per line")
    ap.add_argument("--limit", type=int, help="compare at most this many norms")
    ap.add_argument("--out", type=Path, default=Path("similarity_subset.csv"),
                    help="output CSV (default: %(default)s)")
    ap.add_argument("--top", type=int, default=20, help="pairs to print (default: %(default)s)")
    args = ap.parse_args(argv)

    try:
        records = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(records, list) or not all(isinstance(r, dict) and "norm_id" in r for r in records):
            raise ValueError("expected a JSON array of records that each have a 'norm_id'")
        ids = list(args.ids or [])
        if args.ids_file:
            ids += [line.strip() for line in args.ids_file.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
        subset = select_records(records, ids, args.limit)
    except (OSError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if len(subset) < 2:
        print("ERROR: need at least 2 norms to compare.", file=sys.stderr)
        return 1
    if len(subset) > MAX_NORMS:
        print(f"ERROR: {len(subset)} norms selected (max {MAX_NORMS}); narrow it with --ids or "
              f"--limit, or use stageA_filtering.py for the full corpus.", file=sys.stderr)
        return 1

    rows = subset_similarities(subset)
    write_csv(args.out, rows)

    print(f"{len(subset)} norms, {len(rows)} pairs -> {args.out}")
    for r in rows[:args.top]:
        score = "  n/a " if r["similarity"] is None else f"{r['similarity']:.4f}"
        print(f"  {score}  {r['norm_id_a']}  <->  {r['norm_id_b']}"
              f"{'   (same article)' if r['same_article'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())