#!/usr/bin/env python3
"""combine_scores.py - combine the object, action and actor scores into one pair score.

This script only SCORES pairs. It does not select candidates: the reference
threshold below is used for the printed summary only. Choose the real cut-off
from a review of the output.

Inputs
------
  object_pair_scores.jsonl   from object_scores.py (one row per pair, norm_a < norm_b)
  object_scores_config.json  from object_scores.py (provides N, needed to normalise)
  AI_ACT_norms.json          norm records, to compute the action score (action_scoring.py)
    or action_verbs.json     output of action_verbs.py (--verbs), same result
  actor_pair_scores.jsonl    from actor_scores.py (keyed by Stage A pair_id)
  actor_scores_config.json   from actor_scores.py (checked: Stage A file, --write mode)
  stageA_pairs.json          the Stage A file recorded in object_scores_config.json;
                             needed to turn the actor file's Stage A pair ids into
                             norm ids (see 6)

Output
------
  combined_pair_scores.jsonl one row per pair, sorted by `combined` (highest first),
                             ties ordered by pair_id (see 5)
  combined_scores_config.json all weights and parameters used, plus row counts

=============================================================================
SCHEME
=============================================================================

    combined = W_ACTION * action_eff + W_OBJECT * object_norm + W_ACTOR * actor

All three components answer the same question - "are these two norms about the same
thing?" - and neither says WHICH relation holds (duplication, contradiction,
exception). That is why they can be added: they are three pieces of evidence for
one claim (same act, same object, same addressee). The relation is decided later (LLM review, modality, conditions).

1. Object component: object_norm = min(1, object_score / O_MAX)

   object_scores.py gives score = spec(k) x w with
       spec(k) = 1 - log(1 + n_k) / log(1 + N).
   A key must occur in at least two norms to be shared, so the highest score
   any pair can reach is
       O_MAX = 1 - log(3) / log(1 + N)        (0.83 at N=700, 0.87 at N=5000).
   Dividing by O_MAX puts the object score on the same 0..1 scale as the action
   score, and keeps it stable as documents are added (N grows, O_MAX grows with
   it). After normalisation, a head-head match on a key shared by exactly two
   norms scores 1.0, whatever the corpus size. The min(1, .) only matters for
   --combine noisy_or, which can exceed O_MAX.

   The object score is never negative: no shared key is weak evidence of a
   difference (paraphrase and stemming can hide a real match), so it is 0,
   not a penalty.

2. Action component: action_eff = action_score, x GENERIC_FACTOR if generic

   action_score (action_scoring.py) is polarity-blind:
       1.0 same axis or identical lemma | 0.5 related axes |
      -0.5 unrelated verbs              | 0.0 missing (no lemma / no verb found)
   Positive matches on generic axes only (ensure, assess, perform, ...) are
   multiplied by GENERIC_FACTOR = 0.7: "both norms ask to ensure something"
   says little, so such pairs need more object evidence to rank high.
   Negative and zero scores are not changed.

3. Actor component: actor = actor score from actor_scores.py, used as is

       1.0 same | 0.7 nested | 0.2 weak | 0 unknown / no actor | -0.25 different

   It is already on the 0..1 scale of the other components (with a small
   negative floor, like the action score), so it is not rescaled. If
   actor_scores.py was run with other --s-* level scores, those are used as
   written; the rule check in 4 reads them from actor_scores_config.json.
   Pairs missing from the actor file score 0 (unknown). 'different' is a small
   penalty, not a block: provider and deployer duties on the same object are
   usually a division of labour, but they can still interact, and a mapping
   error should not hide a pair. The actor direction (a_narrower / b_narrower)
   is kept for routing (lex specialis) and, like object_direction, does not
   enter the score.

4. Weights: W_ACTION = 0.35, W_OBJECT = 0.55, W_ACTOR = 0.10  (sum 1.0)

   The weights still sum to 1, so `combined` keeps its range and the reference
   threshold T = 0.5 keeps its meaning. They were chosen against these rules
   (checked at run time by check_weight_rules(); a warning names any rule a
   --w-* override breaks):

   a) No object evidence, no candidate. The same verb and the same actor
      alone must stay below T:     W_ACTION + W_ACTOR * 1.0 < T     (0.45 < 0.5)
      Same verb, same addressee, unrelated objects ("the provider shall
      notify X" / "... notify Y") is the commonest pattern in a single act and
      is not a candidate. This rule caps W_ACTOR at T - W_ACTION.
   b) Unrelated verbs block a pair, even with a perfect object match and the
      same actor:   W_OBJECT + W_ACTOR - 0.5 * W_ACTION < T         (0.475 < 0.5)
      With weights summing to 1 this needs W_ACTION > 1/3, which is why the
      action weight drops only from 0.40 to 0.35.
   c) A missing verb needs a near-perfect object match. The object alone must
      still be able to carry a pair:            W_OBJECT >= T        (0.55)
      i.e. object_norm >= 0.91 with an unknown actor, >= 0.73 with the same
      actor (was 0.83 without actors).
   d) A different actor lowers a pair but does not block it:
                    W_ACTION + W_OBJECT - 0.25 * W_ACTOR >= T        (0.875 >= 0.5)

   Why the actor weight is the smallest:
   - Low discrimination: within one act most duties address the same few
     actors (provider, deployer, Member State), so 'same' is very common,
     like a shared action axis. It moves pairs within a level; the object score
     must still decide the rank.
   - Coverage: many norms have no usable actor (score 0). A large weight would
     make the ranking depend on mapping coverage rather than on content.
   - Rule a leaves little room: W_ACTOR < 0.5 - W_ACTION, and rule b needs
     W_ACTION > 1/3, so W_ACTOR < 0.17 in any case. 0.10 keeps a margin under
     both rules (a: 0.05, b: 0.025).
   The object weight is still the largest because it is the discriminating
   signal: the action and actor scores have only a few levels each.
   Swing of the actor term: same vs different = 0.125, worth about 0.23 of
   object_norm.

   Object score a pair needs to reach T = 0.5 (n/r = not reachable):

       action level                  actor: same  nested  weak  unknown  diff.
       1.0 (specific)                       0.09    0.15  0.24     0.27  0.32
       1.0 (generic, eff 0.7)               0.28    0.34  0.43     0.46  0.51
       0.5 (specific)                       0.41    0.46  0.55     0.59  0.64
       0.5 (generic, eff 0.35)              0.50    0.56  0.65     0.69  0.73
       0.0 (missing)                        0.73    0.78  0.87     0.91  0.95
      -0.5 (unrelated)                       n/r     n/r   n/r      n/r   n/r
   (Without actors, the 'unknown' column was 0.17 / 0.37 / 0.50 / 0.60 / 0.83.)

   Range of `combined`: -0.2 (unrelated verbs, no shared object key, different
                              actor)
                     to  1.0 (same specific verb, rarest possible object key,
                              same actor).

   All weights are starting points. Calibrate them on a labelled sample (as
   with validate_actions.py) before fixing a threshold.

5. Which pairs are scored

   Exactly the pairs in object_pair_scores.jsonl. Stage A is applied upstream:
   object_scores.py only scores pairs listed in stageA_pairs.json, so pairs Stage
   A discarded (same article, low cosine) cannot enter here. A Stage A pair that
   is absent from the object file has object score 0 and can reach at most 0.45
   (rule a), so it can never rank above the reference threshold. Actor rows for
   pairs not in the object file are therefore ignored.
   The script stops if object_scores_config.json does not record a Stage A
   file, i.e. if the object file was produced without the Stage A restriction.

6. Pair identity and order (for the selection and LLM steps)

   Every row carries pair_id = sha256("norm_a::norm_b")[:16], with norm_a < norm_b.
   It is the one key shared by all later steps: LLM cache, finding IDs, and the
   hash-based calibration sample. Use pair_id() from this module rather than
   re-implementing it, so all steps agree.

   Rows with the same `combined` score are ordered by pair_id, not by norm ID.
   Sorting by norm ID would put whole instruments first at every tie (CELEX ids
   sort before BWBR ids), so a cut-off through a tie group would favour some laws
   systematically. pair_id order is stable across runs but unrelated to source.

   actor_pair_scores.jsonl uses a different key: the pair_id of stageA_pairs.json
   (or the pair's position, for older files), with the norms in Stage A order
   (i, j). Rows are joined through the Stage A file: (i, j) -> (norm_ids[i],
   norm_ids[j]) -> the sorted pair. When Stage A lists the pair as (larger,
   smaller), the actor direction is flipped so that a_narrower / b_narrower are
   relative to norm_a < norm_b, like object_direction. The script stops if the
   actor file was scored against a different Stage A file, or against a Stage A
   file with a different number of pairs (positions would not match). It warns
   if the actor file was written with --write positive (negative scores lost).

Usage
-----
  python combine_scores.py --objects data/object_pair_scores.jsonl \\
      --object-config data/object_scores_config.json --norms AI_ACT_norms.json
  python combine_scores.py --objects ... --verbs action_verbs.json
  python combine_scores.py --objects ... --norms ... --w-action 0.3 --w-object 0.6 --w-actor 0.1
  python combine_scores.py --objects ... --actors data/conflict/actor/actor_pair_scores.jsonl
  python combine_scores.py --objects ... --w-actor 0      # old two-component score;
                                                          # actor file not needed

Reading the output (combined_pair_scores.jsonl):
  pair_id                  stable pair identity (see 6)
  combined                 the pair score (higher = stronger candidate)
  action_score, action_eff raw action score and the value used (after the generic factor)
  action_relation, generic, action_basis, lemma_a, lemma_b
  object_score, object_norm raw and normalised object score
  object_key, object_relation, object_direction, unit_a, unit_b
                           copied from object_pair_scores.jsonl
  actor_score              actor score (0 when the pair is not in the actor file)
  actor_direction          a_narrower | b_narrower for nested actors, else null
  `object_direction` (same | a_narrower | b_narrower) and `actor_direction` are
  relative to norm_a, norm_b and are kept for routing (lex specialis); they do
  not enter the score.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path

from action.action_scoring import (
    ActionProfile,
    build_profiles_from_norms,
    build_profiles_from_verbs,
    score_action_pair,
)

# --- Weights (see the docstring for how they were chosen) ---------------------
W_ACTION = 0.35
W_OBJECT = 0.55
W_ACTOR = 0.10
GENERIC_FACTOR = 0.7
# actor_scores.py defaults; overridden by the level scores in actor_scores_config.json
ACTOR_SAME, ACTOR_DIFFERENT = 1.0, -0.25
ACTION_UNRELATED = -0.5
REFERENCE_THRESHOLD = 0.5      # summary only; this script does not select pairs


def o_max(n_norms: int) -> float:
    """Highest object score a pair can reach: a key shared by exactly two norms."""
    if n_norms < 2:
        return 1.0
    return 1 - math.log(3) / math.log(1 + n_norms)


def effective_action(score: float, generic: bool, generic_factor: float) -> float:
    """Discount positive matches on generic verbs; leave 0 and negatives as they are."""
    return score * generic_factor if generic and score > 0 else score


def check_weight_rules(w_action: float, w_object: float, w_actor: float, t: float,
                       actor_same: float = ACTOR_SAME,
                       actor_different: float = ACTOR_DIFFERENT) -> list[str]:
    """Rules a-d of the docstring (section 4). Returns the broken ones."""
    broken = []
    if not w_action * 1.0 + w_actor * actor_same < t:
        broken.append("rule a) same verb + same actor without object evidence reaches T")
    if not w_object + w_actor * actor_same + w_action * ACTION_UNRELATED < t:
        broken.append("rule b) unrelated verbs no longer block a perfect object + same actor")
    if not w_object >= t:
        broken.append("rule c) a missing verb can no longer be carried by the object score")
    if not w_action + w_object + w_actor * actor_different >= t:
        broken.append("rule d) a different actor blocks even a perfect pair")
    if abs(w_action + w_object + w_actor - 1) > 1e-9:
        broken.append("the sum of 1: the range of `combined` and the "
                      "meaning of T change")
    return broken


def load_stage_a_pairs(path: Path) -> dict:
    """stageA_pairs.json -> {stage_a_pair_id: (norm_i, norm_j)} in Stage A order.
    Same format rules as actor_scores.load_stage_a: [pair_id, i, j] or [i, j]
    (pair_id = position)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = [str(v) for v in data["norm_ids"]]
    dupes = sorted(v for v, c in Counter(ids).items() if c > 1)
    if dupes:
        sys.exit(f"{path}: {len(dupes)} duplicate norm_ids (e.g. {dupes[:3]}); "
                 "actor rows cannot be resolved to norms.")
    return {(p[0] if len(p) >= 3 else k): (ids[int(p[-2])], ids[int(p[-1])])
            for k, p in enumerate(data["pairs"])}


def load_actor_scores(actors_path: Path, actor_config: dict, stage_a_path: Path) -> dict:
    """actor_pair_scores.jsonl -> {pair_key: (score, direction)}, with the
    direction made relative to the sorted pair (norm_a < norm_b)."""
    recorded = actor_config.get("stage_a")
    if recorded and Path(recorded).resolve() != stage_a_path.resolve():
        sys.exit(f"{actors_path} was scored against Stage A file {recorded}, the object "
                 f"scores against {stage_a_path}. Re-run actor_scores.py with "
                 f"--stage-a {stage_a_path}.")
    stage_a = load_stage_a_pairs(stage_a_path)
    n_recorded = actor_config.get("stage_a_pairs")
    if n_recorded is not None and n_recorded != len(stage_a):
        sys.exit(f"{actors_path} was scored on {n_recorded} Stage A pairs, {stage_a_path} "
                 f"now has {len(stage_a)}. Re-run actor_scores.py.")
    flip = {"a_narrower": "b_narrower", "b_narrower": "a_narrower"}
    out, unresolved = {}, 0
    for r in read_jsonl(actors_path):
        norms = stage_a.get(r["pair_id"])
        if norms is None:
            unresolved += 1
            continue
        i, j = norms
        direction = r.get("direction")
        if i > j:                       # Stage A order differs from norm_a < norm_b
            direction = flip.get(direction, direction)
        out[pair_key(i, j)] = (float(r["score"]), direction)
    if unresolved:
        sys.exit(f"{unresolved} rows in {actors_path} have a pair_id that is not in "
                 f"{stage_a_path}. Re-run actor_scores.py.")
    return out


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def pair_key(a: str, b: str) -> tuple[str, str]:
    """Order-independent key; object_scores.py writes pairs with norm_a < norm_b."""
    return (a, b) if a <= b else (b, a)


def pair_id(a: str, b: str) -> str:
    """Stable, order-independent pair identity shared by all later steps
    (selection, LLM cache, finding IDs, calibration sample)."""
    x, y = pair_key(a, b)
    return hashlib.sha256(f"{x}::{y}".encode("utf-8")).hexdigest()[:16]


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a sibling temp file, so a killed run never leaves a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main() -> None:
    p = argparse.ArgumentParser(description="Combine object, action and actor scores per norm pair.")
    p.add_argument("--objects", type=Path, default = Path("data/conflict/object/object_pair_scores.jsonl"), required=False,
                   help="object_pair_scores.jsonl from object_scores.py")
    p.add_argument("--object-config", type=Path, default=None,
                   help="object_scores_config.json (default: next to --objects)")
    p.add_argument("--n-norms", type=int, default=None,
                   help="override N used for O_MAX (default: from the object config)")
    p.add_argument("--norms", type=Path, default=Path("data/new_norms/original_norms.json"),
                   help="norm records, to compute action scores")
    p.add_argument("--verbs", type=Path, default=None,
                   help="action_verbs.py output instead of --norms")
    p.add_argument("--actors", type=Path,
                   default=Path("data/conflict/actor/actor_pair_scores.jsonl"),
                   help="actor_pair_scores.jsonl from actor_scores.py")
    p.add_argument("--actor-config", type=Path, default=None,
                   help="actor_scores_config.json (default: next to --actors)")
    p.add_argument("--stage-a", type=Path, default=None,
                   help="stageA_pairs.json (default: the file recorded in the object config)")
    p.add_argument("--out", type=Path, default=Path("data/conflict/combined_pair_scores.jsonl"))
    p.add_argument("--w-action", type=float, default=W_ACTION)
    p.add_argument("--w-object", type=float, default=W_OBJECT)
    p.add_argument("--w-actor", type=float, default=W_ACTOR,
                   help="0 reproduces the two-component score; no actor file needed")
    p.add_argument("--generic-factor", type=float, default=GENERIC_FACTOR)
    p.add_argument("--threshold", type=float, default=REFERENCE_THRESHOLD,
                   help="reference threshold for the summary only (no selection)")
    args = p.parse_args()

    # --- N and O_MAX ----------------------------------------------------------
    config_path = args.object_config or args.objects.with_name("object_scores_config.json")
    object_config = (json.loads(config_path.read_text(encoding="utf-8"))
                     if config_path.exists() else {})
    if not object_config.get("stage_a"):
        sys.exit(f"{config_path} records no Stage A file: the object scores were not "
                 "restricted to Stage A pairs. Re-run object_scores.py.")
    n_norms = args.n_norms or object_config.get("N")
    if not n_norms:
        sys.exit("N unknown: pass --object-config or --n-norms (N from object_scores.py).")
    omax = o_max(n_norms)

    # --- Actor scores ---------------------------------------------------------
    actor_config: dict = {}
    actors: dict = {}
    stage_a_path = args.stage_a or Path(object_config["stage_a"])
    if args.w_actor != 0:
        if not args.actors.is_file():
            sys.exit(f"{args.actors} not found. Run actor_scores.py, or pass --w-actor 0 "
                     "to combine without actors.")
        actor_config_path = args.actor_config or args.actors.with_name("actor_scores_config.json")
        if actor_config_path.is_file():
            actor_config = json.loads(actor_config_path.read_text(encoding="utf-8"))
        else:
            print(f"Warning: {actor_config_path} not found; cannot check that the actor "
                  "scores use the same Stage A file.", file=sys.stderr)
        if actor_config.get("write") == "positive":
            print("Warning: the actor file was written with --write positive; 'different' "
                  "actors (negative scores) are missing and score 0. Re-run actor_scores.py "
                  "with --write nonzero.", file=sys.stderr)
        actors = load_actor_scores(args.actors, actor_config, stage_a_path)

    actor_levels = actor_config.get("scores", {})
    broken = check_weight_rules(args.w_action, args.w_object, args.w_actor, args.threshold,
                                actor_levels.get("same", ACTOR_SAME),
                                actor_levels.get("different", ACTOR_DIFFERENT))
    for rule in broken:
        print(f"Warning: weights break {rule} (see docstring, section 4).",
              file=sys.stderr)

    # --- Action profiles -------------------------------------------------------
    profiles = (build_profiles_from_verbs(args.verbs) if args.verbs
                else build_profiles_from_norms(args.norms))
    missing_profile = ActionProfile("empty", None)

    # --- Object scores ----------------------------------------------------------
    objects = {pair_key(str(r["norm_a"]), str(r["norm_b"])): r for r in read_jsonl(args.objects)}

    pairs = set(objects)

    unknown_ids: set[str] = set()
    rows = []
    for a, b in pairs:
        for n in (a, b):
            if n not in profiles:
                unknown_ids.add(n)
        act = score_action_pair(profiles.get(a, missing_profile),
                                profiles.get(b, missing_profile))
        a_eff = effective_action(act.action_score, act.generic, args.generic_factor)

        obj = objects.get((a, b), {})
        o_raw = float(obj.get("score", 0.0))
        o_norm = min(1.0, o_raw / omax)

        actor_score, actor_direction = actors.get((a, b), (0.0, None))

        combined = (args.w_action * a_eff + args.w_object * o_norm
                    + args.w_actor * actor_score)
        rows.append({
            "pair_id": pair_id(a, b),
            "norm_a": a, "norm_b": b,
            "combined": round(combined, 4),
            "action_score": act.action_score,
            "action_eff": round(a_eff, 4),
            "action_relation": act.action_relation,
            "generic": act.generic,
            "action_basis": act.basis,
            "lemma_a": profiles[a].lemma if a in profiles else None,
            "lemma_b": profiles[b].lemma if b in profiles else None,
            "object_score": o_raw,
            "object_norm": round(o_norm, 4),
            "object_key": obj.get("key"),
            "object_relation": obj.get("relation"),
            "object_direction": obj.get("direction"),
            "unit_a": obj.get("unit_a"),
            "unit_b": obj.get("unit_b"),
            "actor_score": actor_score,
            "actor_direction": actor_direction,
        })

    rows.sort(key=lambda r: (-r["combined"], r["pair_id"]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(args.out, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))

    config = {
        "scheme": ("combined = w_action * action_eff + w_object * min(1, object_score / O_MAX)"
                   " + w_actor * actor_score"),
        "w_action": args.w_action, "w_object": args.w_object, "w_actor": args.w_actor,
        "weight_rules_broken": broken,
        "generic_factor": args.generic_factor,
        "N": n_norms, "O_MAX": round(omax, 4),
        "reference_threshold": args.threshold,
        "pair_id": "sha256('norm_a::norm_b')[:16], norm_a < norm_b",
        "sort": "combined desc, then pair_id",
        "inputs": {"objects": str(args.objects),
                   "actions": str(args.verbs or args.norms),
                   "actors": str(args.actors) if args.w_actor != 0 else None,
                   "stage_a": object_config.get("stage_a")},
        "actor_level_scores": actor_levels or None,
        "counts": {"object_pairs": len(objects), "scored": len(rows),
                   "actor_rows": len(actors),
                   "scored_with_nonzero_actor": sum(r["actor_score"] != 0 for r in rows)},
    }
    atomic_write_text(args.out.with_name("combined_scores_config.json"),
                      json.dumps(config, indent=2))

    # --- Summary (no selection) ---------------------------------------------------
    bands = Counter()
    for r in rows:
        c = r["combined"]
        bands["< 0" if c < 0 else "0.00-0.25" if c < 0.25 else "0.25-0.50" if c < 0.5
              else "0.50-0.75" if c < 0.75 else "0.75-1.00"] += 1
    above = Counter(r["action_relation"] for r in rows if r["combined"] >= args.threshold)
    above_actor = Counter(r["actor_score"] for r in rows if r["combined"] >= args.threshold)

    print(f"Scored {len(rows)} pairs -> {args.out}")
    print(f"Pairs from {args.objects} (restricted to Stage A {object_config['stage_a']})")
    print(f"N = {n_norms}, O_MAX = {omax:.4f}, weights: action {args.w_action}, "
          f"object {args.w_object}, actor {args.w_actor}, generic factor {args.generic_factor}")
    if args.w_actor != 0:
        print(f"Actor rows: {len(actors)}, of which on scored pairs: "
              f"{sum(r['actor_score'] != 0 for r in rows)} (other scored pairs: actor 0)")
    for band in ["< 0", "0.00-0.25", "0.25-0.50", "0.50-0.75", "0.75-1.00"]:
        print(f"  combined {band:>9}: {bands[band]}")
    print(f"At or above reference threshold {args.threshold}: {sum(above.values())} "
          f"(by action relation: {dict(above)})")
    if args.w_actor != 0:
        print(f"  by actor score: {dict(sorted(above_actor.items(), reverse=True))}")
    if unknown_ids:
        print(f"Warning: {len(unknown_ids)} norm IDs in the object file have no action profile "
              f"(scored as missing). Check that both stages use the same norm_id, "
              f"e.g. {sorted(unknown_ids)[:3]}", file=sys.stderr)


if __name__ == "__main__":
    main()