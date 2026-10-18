#!/usr/bin/env python3
"""actor_scores.py - Step 4 of the actor filter: score the actors of norm pairs.

  Actor pair   Each actor of a norm is an actor ID (mapped), a broad class
               (fallback) or nothing. Two actors get the relation of the first
               rule that applies; relations are grouped in five score levels:

               level      score  relations (detail)
               same       1.0    same: same actor ID
               nested     0.7    subtype, subtype2 (a kind of a kind), part_of:
                                 one actor is a kind of, or belongs to, the other
               weak       0.2    sibling: two kinds of the same actor;
                                 broad_covers: a broad actor whose class fits the
                                 mapped other; broad_same_class: two broad actors
                                 of the same class
               unknown    0      no usable actor, two IDs that never share a
                                 document, broad actors of different classes
               different -0.25   both mapped, no relation, and the IDs share a
                                 document

  Norm pair    The best actor pair over all actors of both norms (highest score;
               ties go to the more specific detail). 0 if either norm has no
               usable actor (missing, pronoun, not an actor).
  Direction    For nested pairs: which norm's actor is the narrower one
               (a_narrower / b_narrower), relative to norm_a and norm_b. 'same'
               for same. Does not enter the score.

Relations between IDs come from actor_relations.jsonl (definitions, labels inside
labels, inventory part_of, derived). Hand-made relations (actor_relations_curated.tsv)
are NOT used unless --curated is given. A relation holds between two IDs wherever
the norms come from: IDs already carry their document.

Only pairs that Stage A kept (data/conflict/stageA_pairs.json) are scored.

Reads  <data>/norm_actor_map.jsonl     (actor_map.py)
       <data>/actor_relations.jsonl    (actor_relations.py)
       <data>/actor_inventory.jsonl    (actor_inventory.py; scopes and classes)
       <data>/actor_relations_curated.tsv  (only with --curated)
       data/conflict/stageA_pairs.json (--stage-a)
Writes <data>/actor_pair_scores.jsonl, <data>/actor_scores_config.json

Curated file, only with --curated (tab-separated, '#' starts a comment):
  <actor ID a>  <relation>  <actor ID b>  [note]
  relation: equivalent | instance | subtype | subtype2 | part_of | can_be_same |
            linked | sibling | different
  Direction as written: 'X subtype Y' means X is a kind of Y; 'X part_of Y' means
  X belongs to Y; 'X instance Y' means X is the named body filling role Y.

Usage
-----
Run after actor_map.py and actor_relations.py:

  python actor_scores.py                        # all Stage A pairs with score != 0
  python actor_scores.py --write positive       # only pairs with score > 0
  python actor_scores.py --s-weak 0             # change a level score
  python actor_scores.py --curated actor_relations_curated.tsv   # add hand-made relations
  python actor_scores.py --data other/folder --out pairs_test.jsonl
  python actor_scores.py --stage-a other/stageA_pairs.json

Options: --data, --norms, --relations, --inventory, --curated, --stage-a, --out,
--write {nonzero,positive,all}, --details, --s-same, --s-nested, --s-weak, --s-different,
--threshold (summary only). All are saved in actor_scores_config.json.

Norm IDs: norm_actor_map.jsonl and stageA_pairs.json must come from the same norm
list. The run stops if Stage A has duplicate norm_ids, or if norm_actor_map.jsonl
has norm_ids that Stage A does not know.

Reading the output (actor_pair_scores.jsonl, one row per Stage A pair):
  {"pair_id": 1234, "score": 0.7, "direction": "a_narrower"}
  pair_id    pair_id from stageA_pairs.json (for older files without pair ids:
             the position of the pair in the list).
  score      actor score: 1.0 same | 0.7 nested | 0.2 weak | -0.25 different
             (with the default level scores; 0 = unknown is not written).
  direction  only for nested pairs: "a_narrower" if the actor of the pair's first
             norm (i in Stage A) is the narrower one, "b_narrower" if the
             second's (j) is.
  - Pairs not in the file score 0. Negative scores ARE written (default
    --write nonzero): they are evidence that the norms address different actors.
  - To see why a pair got its score, run with --details (writes the actors,
    relation and source too; for checking only, the file is much larger), or
    call ActorScorer.norm_pair in other code.

Using the score:
  The score says how likely two norms address the same (or an overlapping)
  actor. 'same' points to duplication or a direct conflict; 'nested' with a
  direction points to lex specialis (the narrower actor's norm may be the
  special rule); 'different' lowers the pair. Thresholds and how this
  score combines with the object score should be set from the pair review.

In other code:
  from actor_scores import ActorScorer, load_norm_actors, load_relations, load_stage_a
  norms = load_norm_actors("data/conflict/actor/norm_actor_map.jsonl")
  scorer = ActorScorer(norms, load_relations(...), inventory_entries)
  scorer.norm_pair(norm_id_a, norm_id_b)        # full detail: score, level, relation,
                                                # actors, source, direction

Recompute after any change to the inventory, the relations or the mapping.
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    from actor_lexicon import in_scope
except ImportError:  # keep the scorer usable without the inventory module
    in_scope = None

# --- Score levels -------------------------------------------------------------
DEFAULT_SCORES = {"same": 1.0, "nested": 0.7, "weak": 0.2, "unknown": 0.0, "different": -0.25}
# relation (detail) -> level; curated-only relations are mapped too, for --curated
LEVEL = {"same": "same", "equivalent": "same",
         "subtype": "nested", "subtype2": "nested", "part_of": "nested", "instance": "nested",
         "sibling": "weak", "broad_covers": "weak", "broad_same_class": "weak",
         "can_be_same": "weak", "linked": "weak",
         "unknown": "unknown", "different": "different"}
# order for ties within a level: the more specific detail first
PRIORITY = ["same", "equivalent", "instance", "subtype", "part_of", "subtype2",
            "can_be_same", "linked", "broad_covers", "sibling", "broad_same_class",
            "unknown", "different"]
DIRECTED = {"subtype", "subtype2", "part_of", "instance"}
CURATED_RELATIONS = {"equivalent", "instance", "subtype", "subtype2", "part_of",
                     "can_be_same", "linked", "sibling", "different"}
# a broad actor of class X may cover a mapped actor of these classes
COVERS = {"authority": {"authority"}, "body": {"body"}, "institution": {"institution"},
          "state": {"state"}, "office": {"office"}, "party": {"party"},
          "person": {"person", "party"}}

# --- Input field names --------------------------------------------------------
F = dict(norm_id="norm_id", document="document", status="status", actors="actors",
         actor_id="actor_id", fallback_class="fallback_class", unit="unit")


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_norm_actors(path):
    """norm_actor_map.jsonl -> {norm_id: (document, [actor, ...])}, where an actor
    is ('id', actor_id, unit) or ('class', class, unit). Norms without a usable
    actor get an empty list."""
    out = {}
    for r in read_jsonl(path):
        actors = []
        for a in r.get(F["actors"]) or []:
            if a.get(F["actor_id"]):
                actors.append(("id", a[F["actor_id"]], a.get(F["unit"])))
            elif a.get(F["fallback_class"]):
                actors.append(("class", a[F["fallback_class"]], a.get(F["unit"])))
        out[str(r[F["norm_id"]])] = (r.get(F["document"]), actors)
    return out


def load_relations(path, curated=None):
    """{(a, b): (relation, source)} for both orders; for directed relations the
    reverse order is stored as '<relation>~rev'. Curated lines override."""
    rel = {}

    def put(a, b, r, src):
        rel[(a, b)] = (r, src)
        rel[(b, a)] = (r + "~rev" if r in DIRECTED else r, src)

    for r in read_jsonl(path) if Path(path).is_file() else []:
        put(r["a"], r["b"], r["relation"], r.get("source", "relations"))
    if curated and Path(curated).is_file():
        for n, line in enumerate(Path(curated).read_text(encoding="utf-8").splitlines(), 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split("\t")]
            if len(parts) < 3 or parts[1] not in CURATED_RELATIONS:
                raise ValueError(f"{curated}:{n}: expected '<id>\\t<relation>\\t<id>', "
                                 f"relation one of {sorted(CURATED_RELATIONS)}")
            put(parts[0], parts[2], parts[1], "curated")
    return rel


def load_stage_a(path):
    """stageA_pairs.json -> (norm_ids, [(pair_id, i, j), ...]). Pairs are
    [pair_id, i, j] (current format) or [i, j] (older files, pair_id = position);
    i and j index norm_ids."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    ids = [str(v) for v in data["norm_ids"]]
    dupes = sorted(v for v, c in Counter(ids).items() if c > 1)
    if dupes:
        raise ValueError(f"{path}: {len(dupes)} duplicate norm_ids (e.g. {dupes[:3]}); "
                         "pairs cannot be resolved to one norm each. Fix the IDs "
                         "upstream (merge_norms.py).")
    pairs = [(p[0] if len(p) >= 3 else k, int(p[-2]), int(p[-1]))
             for k, p in enumerate(data["pairs"])]
    return ids, pairs


class ActorScorer:
    def __init__(self, norms, relations, entries, scores=None):
        self.norms = norms
        self.rel = relations
        self.s = dict(DEFAULT_SCORES, **(scores or {}))
        self.cls = {e["id"]: e.get("class") for e in entries}
        # documents in which each ID is valid (for 'different' versus 'unknown')
        docs = sorted({d for d, _ in norms.values() if d})
        by_id = {e["id"]: e for e in entries}
        self.valid = defaultdict(set)
        for e in entries:
            for d in docs:
                if in_scope is not None and in_scope(e, d):
                    self.valid[e["id"]].add(d)
        for d, actors in norms.values():   # IDs seen in a document are valid there
            for kind, v, _ in actors:
                if kind == "id":
                    self.valid[v].add(d)
        self._cache = {}

    def actor_pair(self, x, y):
        """(relation, score, source, direction) for two actors (kind, value)."""
        (kx, vx), (ky, vy) = x, y
        if kx == ky == "id":
            if vx == vy:
                return "same", "id", "same"
            r = self.rel.get((vx, vy))
            if r:
                name, src = r
                if name.endswith("~rev"):
                    return name[:-4], src, "b_narrower"
                return name, src, "a_narrower" if name in DIRECTED else (
                    "same" if name == "equivalent" else "")
            if self.valid[vx] & self.valid[vy]:
                return "different", "scope", ""
            return "unknown", "scope", ""
        if kx == ky == "class":
            return ("broad_same_class" if vx == vy else "unknown"), "class", ""
        broad, other = (vx, vy) if kx == "class" else (vy, vx)
        if self.cls.get(other) in COVERS.get(broad, {broad}):
            return "broad_covers", "class", ("b_narrower" if kx == "class" else "a_narrower")
        return "unknown", "class", ""

    def norm_pair(self, na, nb):
        """Best actor pair of two norms; score 0 if either has no usable actor."""
        _, xa = self.norms.get(na, (None, []))
        _, xb = self.norms.get(nb, (None, []))
        base = {"norm_a": na, "norm_b": nb, "score": 0.0}
        if not xa or not xb:
            return base
        key = (tuple(a[:2] for a in xa), tuple(b[:2] for b in xb))
        if key not in self._cache:
            best = None
            for i, x in enumerate(xa):
                for j, y in enumerate(xb):
                    rel, src, direc = self.actor_pair(x[:2], y[:2])
                    cand = (self.s[LEVEL[rel]], -PRIORITY.index(rel), i, j, rel, src, direc)
                    if best is None or cand[:2] > best[:2]:
                        best = cand
            self._cache[key] = best
        s, _, i, j, rel, src, direc = self._cache[key]
        fmt = lambda a: a[1] if a[0] == "id" else f"class:{a[1]}"
        return {**base, "score": round(s, 4), "level": LEVEL[rel], "relation": rel, "source": src,
                "actor_a": fmt(xa[i]), "actor_b": fmt(xb[j]),
                "unit_a": xa[i][2], "unit_b": xb[j][2], "direction": direc}


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--data", default="data/conflict/actor")
    p.add_argument("--norms", default="norm_actor_map.jsonl")
    p.add_argument("--relations", default="actor_relations.jsonl")
    p.add_argument("--inventory", default="actor_inventory.jsonl")
    p.add_argument("--curated", default=None,
                   help="hand-made relations (tsv); not used unless given")
    p.add_argument("--stage-a", default="data/conflict/stageA_pairs.json",
                   help="Stage A output; only these pairs are scored")
    p.add_argument("--out", default="actor_pair_scores.jsonl")
    p.add_argument("--write", choices=["nonzero", "positive", "all"], default="nonzero",
                   help="which scored pairs to write (default: score != 0)")
    p.add_argument("--details", action="store_true",
                   help="also write norm ids, actors, relation and source (for checking)")
    for name, val in DEFAULT_SCORES.items():
        if name != "unknown":
            p.add_argument(f"--s-{name.replace('_', '-')}", type=float, default=val,
                           dest=f"s_{name}")
    p.add_argument("--threshold", type=float, default=0.5,
                   help="relevance threshold, used for the summary only")
    args = p.parse_args()

    data = Path(args.data)
    scores = {n: getattr(args, f"s_{n}") for n in DEFAULT_SCORES if n != "unknown"}
    norms = load_norm_actors(data / args.norms)
    try:
        curated = (data / args.curated) if args.curated else None
        relations = load_relations(data / args.relations, curated)
    except ValueError as e:
        sys.exit(str(e))
    entries = read_jsonl(data / args.inventory) if (data / args.inventory).is_file() else []
    if not entries:
        print(f"Note: {data / args.inventory} not found; IDs are 'different' only when "
              "they occur in the same document.")
    try:
        stage_a = load_stage_a(args.stage_a)
    except (OSError, ValueError, KeyError) as e:
        sys.exit(f"Cannot read Stage A pairs: {e}")
    ids, idx_pairs = stage_a
    unknown_ids = sorted(set(norms) - set(ids))
    if unknown_ids:
        sys.exit(f"{len(unknown_ids)} norm_ids in {data / args.norms} are not in Stage A "
                 f"({args.stage_a}), e.g. {unknown_ids[:3]}. The two files come from "
                 "different norm lists; regenerate one of them.")
    scorer = ActorScorer(norms, relations, entries, scores)
    usable = {n for n, (_, a) in norms.items() if a}
    not_in_map = len(set(ids) - set(norms))

    rels, levels, bands, directions = Counter(), Counter(), Counter(), Counter()
    written = above = negative = zero = 0
    with open(data / args.out, "w", encoding="utf-8") as f:
        for pid, i, j in idx_pairs:
            r = scorer.norm_pair(ids[i], ids[j])
            sc = r["score"]
            rels[r.get("relation", "no actor")] += 1
            levels[r.get("level", "no actor")] += 1
            if sc == 0:
                zero += 1
            elif sc < 0:
                negative += 1
            else:
                above += sc >= args.threshold
                bands[min(int(sc * 4), 3)] += 1
                directions[r.get("direction") or "none"] += 1
            if args.write == "all" or (args.write == "nonzero" and sc != 0) or \
                    (args.write == "positive" and sc > 0):
                row = {"pair_id": pid, "score": sc}
                if r.get("level") == "nested" and r.get("direction") in ("a_narrower", "b_narrower"):
                    row["direction"] = r["direction"]
                if args.details:
                    row.update({k: v for k, v in r.items() if k not in ("score", "direction")})
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                written += 1

    config = vars(args) | {"scores": scores, "stage_a_pairs": len(idx_pairs),
                           "norms_with_actor": len(usable),
                           "stage_a_norms_not_in_actor_map": not_in_map,
                           "relations_loaded": len(relations) // 2}
    (data / "actor_scores_config.json").write_text(json.dumps(config, indent=2))

    n = len(idx_pairs) or 1
    print(f"Norms: {len(norms)}  with a usable actor: {len(usable)}  "
          f"relations between IDs: {len(relations) // 2}")
    print(f"Stage A pairs: {len(idx_pairs)}  written: {written} (--write {args.write})")
    if not_in_map:
        print(f"Note: {not_in_map} Stage A norms are not in {args.norms}; their pairs score 0.")
    print(f"  positive: {sum(bands.values())} ({100 * sum(bands.values()) / n:.1f}%)  "
          f"zero: {zero} ({100 * zero / n:.1f}%)  negative: {negative} ({100 * negative / n:.1f}%)")
    for k, label in enumerate(["0.00-0.25", "0.25-0.50", "0.50-0.75", "0.75-1.00"]):
        print(f"  score {label}: {bands[k]}")
    print(f"Pairs at or above threshold {args.threshold}: {above}")
    print("Levels of the best actor pair: " + ", ".join(
        f"{k} {levels[k]} ({100 * levels[k] / n:.1f}%)"
        for k in ["same", "nested", "weak", "unknown", "different", "no actor"] if levels[k]))
    print("Details: " + ", ".join(
        f"{k} {v} ({100 * v / n:.1f}%)" for k, v in sorted(
            rels.items(), key=lambda kv: PRIORITY.index(kv[0]) if kv[0] in PRIORITY else 99)))
    print(f"Direction flags (positive pairs): {dict(directions)}")


if __name__ == "__main__":
    main()