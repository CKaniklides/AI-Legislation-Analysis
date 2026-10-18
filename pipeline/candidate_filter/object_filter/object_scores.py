#!/usr/bin/env python3
"""object_scores.py - Steps 2-5 of the object filter.

  Step 2  Specificity per key: spec(k) = 1 - log(1 + n_k) / log(1 + N)
          n_k = norms with key k, N = norms with at least one unit
          (computed from the data; norms with "units": [] are left out).
          Word keys (head, tail, other nouns) are counted per word, so the
          same stem gets one specificity whatever its role; EuroVoc concepts
          are counted in their own namespace.
  Step 3  Unit pair score: highest spec(k) x w over shared keys, with
          w = head-head 1.0 | same EuroVoc concept 0.9 |
              compound tail (tail-head, tail-tail) 0.8 | any other shared noun 0.7
  Step 4  Norm pair score: highest unit pair score; 0 if either norm has no units.
  Step 5  Direction flag for the best unit pair (does not enter the score):
          a compound of the other's head, or the same head with a strict
          superset of other nouns, is narrower.

Only pairs that Stage A kept (data/conflict/stageA_pairs.json) are scored.
Of those, only pairs whose norms share at least one key are compared; all
other pairs score 0. Specificity (Step 2) is still computed over ALL norms, so
the Stage A restriction changes which pairs are written, never their scores.

Reads  <data>/norm_keys.jsonl (units inline; unit_keys.jsonl is not needed)
       data/conflict/stageA_pairs.json (--stage-a)
Writes <data>/key_specificity.csv, <data>/object_pair_scores.jsonl,
       <data>/object_scores_config.json

Usage
-----
Run after object_keys.py:

  python object_scores.py                          # all pairs with score > 0
  python object_scores.py --min-score 0.3          # smaller file, same top pairs
  python object_scores.py --w-eurovoc 0.7          # change a key weight
  python object_scores.py --combine noisy_or       # test accumulating shared keys
  python object_scores.py --data other/folder --out pairs_test.jsonl
  python object_scores.py --stage-a other/stageA_pairs.json

Options: --data, --norms, --stage-a, --out, --spec-out, --w-head, --w-eurovoc,
--w-tail, --w-noun, --combine {max,noisy_or}, --min-score, --threshold (summary
only), --n-norms (override N). All are saved in object_scores_config.json.

Norm IDs: norm_keys.jsonl and stageA_pairs.json must come from the same norm
list. The run stops if Stage A has duplicate norm_ids, or if norm_keys.jsonl
has norm_ids that Stage A does not know. Stage A norms without object keys are
only counted (they score 0).

Reading the output (object_pair_scores.jsonl, one row per pair):
  score      object similarity in [0, 1]; 0 = no evidence, never a penalty.
  key, relation, spec, weight
             the shared key that decided the score and how it was weighted.
  unit_a/b   the units that matched (unit text).
  direction  same | a_narrower | b_narrower | "" (relative to norm_a, norm_b).
  - Each pair appears once, with norm_a < norm_b; the score is symmetric.
  - A pair that is not in the file scores 0 (discarded by Stage A, no shared
    key, or a norm without units). Treat missing pairs as 0 when combining.
  - With --min-score, every pair at or above that score is still found.

Using the score:
  The score says how strongly two norms concern the same thing. It does not
  say which relation holds; modality, verb and condition filters decide that.
  Use it for both duplications and contradictions, and route by direction:
  "same" points to duplication (or a conflict in values such as deadlines or
  amounts, so still check conditions); "a_narrower"/"b_narrower" points to
  lex specialis. Thresholds may need to differ per relation: set them from
  the pair review, not from this script's --threshold.

In other code:
  from object_scores import ObjectScorer, load_norm_keys
  units, norm_units = load_norm_keys("data/eurovoc/norm_keys.jsonl")
  scorer = ObjectScorer(units, norm_units)        # same defaults as the CLI
  scorer.norm_pair(norm_id_a, norm_id_b)          # dict with score, key, direction
  stage_a = load_stage_a("data/conflict/stageA_pairs.json")
  scorer.candidate_pairs(stage_a=stage_a)         # Stage A pairs sharing a key
  scorer.spec                                     # {"w:<stem>" | "ev:<uri>": spec}

Recompute after any change to norm_keys.jsonl, including when new legislation
is added: N and every n_k change, so all specificities and scores shift.
"""
import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

# --- Input: norm_keys.jsonl from object_keys.py --------------------------------
# One row per norm, with its units inline:
#   {"norm_id": ..., "units": [{"unit": text, "head": stem, "tail": stem | null,
#                               "nouns": [stems], "concept": uri | null}, ...]}
# A unit is identified by its text; identical texts share the same keys.
# Field names can be adapted here.
F = dict(norm_id="norm_id", units="units", unit="unit", head="head",
         tail="tail", nouns="nouns", concept="concept")

DEFAULT_WEIGHTS = {"head": 1.0, "eurovoc": 0.9, "tail": 0.8, "noun": 0.7}
HEAD_TAIL = {"head", "tail"}


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def make_unit(r):
    head = r.get(F["head"]) or None
    tail = r.get(F["tail"]) or None
    nouns = frozenset(r.get(F["nouns"]) or [])
    concepts = r.get(F["concept"])
    if concepts is None:
        concepts = []
    elif isinstance(concepts, str):
        concepts = [concepts]
    roles = defaultdict(set)
    if head:
        roles[head].add("head")
    if tail:
        roles[tail].add("tail")
    for n in nouns:
        roles[n].add("noun")
    return {"head": head, "tail": tail, "nouns": nouns,
            "eurovoc": frozenset(str(c) for c in concepts),
            "roles": {w: frozenset(rs) for w, rs in roles.items()}}


def load_norm_keys(path):
    """Returns (units {unit text: unit}, norm_units {norm_id: [unit texts]})."""
    units, norm_units = {}, {}
    for r in read_jsonl(path):
        ids = []
        for u in r.get(F["units"]) or []:
            uid = u[F["unit"]]
            if uid not in units:
                units[uid] = make_unit(u)
            if uid not in ids:
                ids.append(uid)
        norm_units[str(r[F["norm_id"]])] = ids
    return units, norm_units


def load_stage_a(path):
    """stageA_pairs.json -> (norm_ids, [(i, j), ...]). Pairs are [pair_id, i, j]
    (current format) or [i, j] (older files); i and j index norm_ids."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    ids = [str(v) for v in data["norm_ids"]]
    dupes = sorted(v for v, c in Counter(ids).items() if c > 1)
    if dupes:
        raise ValueError(f"{path}: {len(dupes)} duplicate norm_ids (e.g. {dupes[:3]}); "
                         "pairs cannot be resolved to one norm each. Fix the IDs "
                         "upstream (merge_norms.py).")
    pairs = [(int(p[-2]), int(p[-1])) for p in data["pairs"]]
    return ids, pairs


def unit_key_set(u):
    return {f"w:{w}" for w in u["roles"]} | {f"ev:{c}" for c in u["eurovoc"]}


def relation(ra, rb):
    if ra == rb == "head":
        return "head"
    if ra in HEAD_TAIL and rb in HEAD_TAIL:
        return "tail"
    return "noun"


def direction(a, b):
    """Step 5, relative to (a, b): 'a_narrower', 'b_narrower', 'same' or ''."""
    if a["head"] != b["head"]:
        if a["tail"] and a["tail"] == b["head"]:
            return "a_narrower"
        if b["tail"] and b["tail"] == a["head"]:
            return "b_narrower"
        return ""
    if a["nouns"] == b["nouns"]:
        return "same"
    if a["nouns"] > b["nouns"]:
        return "a_narrower"
    if b["nouns"] > a["nouns"]:
        return "b_narrower"
    return ""


class ObjectScorer:
    def __init__(self, units, norm_units, weights=None, combine="max", n_norms=None):
        self.units = units
        self.w = dict(DEFAULT_WEIGHTS, **(weights or {}))
        self.combine = combine
        # Norms without (known) units drop out: they score 0 against everything.
        self.norm_units = {}
        for n, us in norm_units.items():
            us = [u for u in us if u in units]
            if us:
                self.norm_units[n] = us
        # Step 2: specificity per key
        self.norm_keys = {n: set().union(*(unit_key_set(units[u]) for u in us))
                          for n, us in self.norm_units.items()}
        self.n_k = Counter(k for ks in self.norm_keys.values() for k in ks)
        self.N = n_norms or len(self.norm_keys)
        denom = math.log(1 + self.N) if self.N > 0 else 1.0
        self.spec = {k: 1 - math.log(1 + n) / denom for k, n in self.n_k.items()}

    def unit_pair(self, a, b):
        """Step 3. Returns (score, best evidence) or (0.0, None)."""
        evidence = []  # (score, key, relation, spec, weight)
        for word in a["roles"].keys() & b["roles"].keys():
            sp = self.spec[f"w:{word}"]
            rel = max((relation(ra, rb) for ra in a["roles"][word] for rb in b["roles"][word]),
                      key=lambda r: self.w[r])
            evidence.append((sp * self.w[rel], word, rel, sp, self.w[rel]))
        same_head = a["head"] is not None and a["head"] == b["head"]
        for c in a["eurovoc"] & b["eurovoc"]:
            if self.combine == "noisy_or" and same_head:
                continue  # concept of a shared head adds no independent evidence
            sp = self.spec[f"ev:{c}"]
            evidence.append((sp * self.w["eurovoc"], c, "eurovoc", sp, self.w["eurovoc"]))
        if not evidence:
            return 0.0, None
        evidence.sort(key=lambda e: (-e[0], e[1]))
        if self.combine == "max":
            return evidence[0][0], evidence[0]
        return 1 - math.prod(1 - e[0] for e in evidence), evidence[0]

    def norm_pair(self, na, nb):
        """Steps 4-5. Score any norm pair; 0 if either norm has no units."""
        best = None
        for x in self.norm_units.get(na, []):
            for y in self.norm_units.get(nb, []):
                s, ev = self.unit_pair(self.units[x], self.units[y])
                if ev and (best is None or s > best[0]):
                    best = (s, x, y, ev)
        if best is None:
            return {"norm_a": na, "norm_b": nb, "score": 0.0}
        s, x, y, ev = best
        return {"norm_a": na, "norm_b": nb, "score": round(s, 4),
                "unit_a": x, "unit_b": y, "key": ev[1], "relation": ev[2],
                "spec": round(ev[3], 4), "weight": ev[4],
                "direction": direction(self.units[x], self.units[y])}

    def candidate_pairs(self, min_score=0.0, stage_a=None):
        """Norm pairs sharing a key, as (norm_a, norm_b) with norm_a < norm_b.
        With combine=max, keys with spec below min_score cannot lift a pair to
        min_score and are skipped. With stage_a (from load_stage_a), only those
        pairs are considered; without it, every pair of norms is (library use)."""
        floor = min_score if self.combine == "max" else 0.0
        if stage_a is not None:
            ids, idx_pairs = stage_a
            out = []
            for i, j in idx_pairs:
                a, b = ids[i], ids[j]
                ka, kb = self.norm_keys.get(a), self.norm_keys.get(b)
                if ka and kb and any(self.spec[k] >= floor for k in ka & kb):
                    out.append((a, b) if a < b else (b, a))
            return sorted(out)
        index = defaultdict(list)
        for n in sorted(self.norm_keys):
            for k in self.norm_keys[n]:
                if self.spec[k] >= floor:
                    index[k].append(n)
        pairs = set()
        for norms in index.values():
            pairs.update(combinations(norms, 2))
        return sorted(pairs)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--data", default="data/conflict/object")
    p.add_argument("--norms", default="norm_keys.jsonl")
    p.add_argument("--stage-a", default="data/conflict/stageA_pairs.json",
                   help="Stage A output; only these pairs are scored")
    p.add_argument("--out", default="object_pair_scores.jsonl")
    p.add_argument("--spec-out", default="key_specificity.csv")
    p.add_argument("--w-head", type=float, default=DEFAULT_WEIGHTS["head"])
    p.add_argument("--w-eurovoc", type=float, default=DEFAULT_WEIGHTS["eurovoc"])
    p.add_argument("--w-tail", type=float, default=DEFAULT_WEIGHTS["tail"])
    p.add_argument("--w-noun", type=float, default=DEFAULT_WEIGHTS["noun"])
    p.add_argument("--combine", choices=["max", "noisy_or"], default="max",
                   help="how shared keys of one unit pair combine (design: max)")
    p.add_argument("--min-score", type=float, default=0.0,
                   help="write only pairs with score >= this (and > 0)")
    p.add_argument("--threshold", type=float, default=0.5,
                   help="relevance threshold, used for the summary only")
    p.add_argument("--n-norms", type=int, default=None,
                   help="override N (default: norms with at least one unit)")
    args = p.parse_args()

    data = Path(args.data)
    weights = {"head": args.w_head, "eurovoc": args.w_eurovoc,
               "tail": args.w_tail, "noun": args.w_noun}
    units, norm_units = load_norm_keys(data / args.norms)
    try:
        stage_a = load_stage_a(args.stage_a)
    except (OSError, ValueError, KeyError) as e:
        sys.exit(f"Cannot read Stage A pairs: {e}")
    stage_ids = set(stage_a[0])
    unknown = sorted(set(norm_units) - stage_ids)
    if unknown:
        sys.exit(f"{len(unknown)} norm_ids in {data / args.norms} are not in Stage A "
                 f"({args.stage_a}), e.g. {unknown[:3]}. The two files come from "
                 "different norm lists; regenerate one of them.")
    scorer = ObjectScorer(units, norm_units, weights, args.combine, args.n_norms)
    no_keys = len(stage_ids - set(scorer.norm_keys))

    with open(data / args.spec_out, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["key", "type", "n_norms", "spec"])
        for k, n in sorted(scorer.n_k.items(), key=lambda kv: (-kv[1], kv[0])):
            wr.writerow([k[k.index(":") + 1:], "eurovoc" if k.startswith("ev:") else "word",
                         n, round(scorer.spec[k], 4)])

    candidates = scorer.candidate_pairs(args.min_score, stage_a=stage_a)
    bands, relations, directions, written, above = Counter(), Counter(), Counter(), 0, 0
    with open(data / args.out, "w", encoding="utf-8") as f:
        for na, nb in candidates:
            r = scorer.norm_pair(na, nb)
            if r["score"] <= 0 or r["score"] < args.min_score:
                continue
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            written += 1
            above += r["score"] >= args.threshold
            bands[min(int(r["score"] * 4), 3)] += 1
            relations[r["relation"]] += 1
            directions[r["direction"] or "none"] += 1

    config = vars(args) | {"N": scorer.N, "weights": weights,
                           "stage_a_pairs": len(stage_a[1]),
                           "stage_a_norms_without_keys": no_keys}
    (data / "object_scores_config.json").write_text(json.dumps(config, indent=2))

    print(f"Norms: {len(norm_units)}  with units (N): {scorer.N}  "
          f"unique units: {len(units)}  keys: {len(scorer.n_k)}")
    print(f"Stage A pairs: {len(stage_a[1])}  sharing a key: {len(candidates)}  "
          f"written: {written}")
    if no_keys:
        print(f"Note: {no_keys} Stage A norms have no object keys; their pairs score 0.")
    for i, label in enumerate(["0.00-0.25", "0.25-0.50", "0.50-0.75", "0.75-1.00"]):
        print(f"  score {label}: {bands[i]}")
    print(f"Pairs at or above threshold {args.threshold}: {above}")
    print(f"Relations of best key: {dict(relations)}")
    print(f"Direction flags: {dict(directions)}")


if __name__ == "__main__":
    main()