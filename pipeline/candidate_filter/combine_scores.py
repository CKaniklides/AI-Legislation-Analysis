#!/usr/bin/env python3
"""combine_scores.py - combine the object and action scores into one pair score.

This script only SCORES pairs. It does not select candidates: the reference
threshold below is used for the printed summary only. Choose the real cut-off
from a review of the output.

Inputs
------
  object_pair_scores.jsonl   from object_scores.py (one row per pair, norm_a < norm_b)
  object_scores_config.json  from object_scores.py (provides N, needed to normalise)
  AI_ACT_norms.json          norm records, to compute the action score (action_scoring.py)
    or action_verbs.json     output of action_verbs.py (--verbs), same result

Output
------
  combined_pair_scores.jsonl one row per pair, sorted by `combined` (highest first),
                             ties ordered by pair_id (see 5)
  combined_scores_config.json all weights and parameters used, plus row counts

=============================================================================
SCHEME
=============================================================================

    combined = W_ACTION * action_eff + W_OBJECT * object_norm

Both components answer the same question - "are these two norms about the same
thing?" - and neither says WHICH relation holds (duplication, contradiction,
exception). That is why they can be added: they are two pieces of evidence for
one claim. The relation is decided later (LLM review, modality, conditions).

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

3. Weights: W_ACTION = 0.4, W_OBJECT = 0.6

   The weights were chosen against three rules, with a reference threshold
   T = 0.5 (the same value used in the summary):

   a) The verb alone must not be enough:      W_ACTION * 1.0 < T      (0.40 < 0.5)
      The same verb on unrelated objects is not a candidate; otherwise every
      pair of notification duties in the corpus would rank high.
   b) Unrelated verbs block a pair, even with a perfect object match:
                                              W_OBJECT - 0.5 * W_ACTION < T  (0.40 < 0.5)
      Different acts on the same object ("provide the documentation" vs
      "retain the documentation") are complementary duties, not duplicates
      or conflicts.
   c) A missing verb needs a near-perfect object match:
                                              W_OBJECT * 0.83 >= T    (object >= 0.83)
      Missing is not evidence against, so the object alone can carry a pair,
      but only when it is very specific.

   The object weight is the larger one because it is the discriminating
   signal: the action score has only four levels, and thousands of pairs share
   an action axis. Within each action level, the object score decides the rank.

   Object score a pair needs to reach T = 0.5, per action level:
       action 1.0 (specific)          object_norm >= 0.17
       action 1.0 (generic, eff 0.7)  object_norm >= 0.37
       action 0.5 (specific)          object_norm >= 0.50
       action 0.5 (generic, eff 0.35) object_norm >= 0.60
       action 0.0 (missing)           object_norm >= 0.83
       action -0.5 (unrelated)        not reachable (max 0.40)

   Range of `combined`: -0.2 (unrelated verbs, no shared object key)
                     to  1.0 (same specific verb, rarest possible object key).

   All weights are starting points. Calibrate them on a labelled sample (as
   with validate_actions.py) before fixing a threshold.

4. Which pairs are scored

   Exactly the pairs in object_pair_scores.jsonl. Stage A is applied upstream:
   object_scores.py only scores pairs listed in stageA_pairs.json, so pairs Stage
   A discarded (same article, low cosine) cannot enter here. A Stage A pair that
   is absent from the object file has object score 0 and can reach at most 0.4
   (rule a), so it can never rank above the reference threshold.
   The script stops if object_scores_config.json does not record a Stage A
   file, i.e. if the object file was produced without the Stage A restriction.

5. Pair identity and order (for the selection and LLM steps)

   Every row carries pair_id = sha256("norm_a::norm_b")[:16], with norm_a < norm_b.
   It is the one key shared by all later steps: LLM cache, finding IDs, and the
   hash-based calibration sample. Use pair_id() from this module rather than
   re-implementing it, so all steps agree.

   Rows with the same `combined` score are ordered by pair_id, not by norm ID.
   Sorting by norm ID would put whole instruments first at every tie (CELEX ids
   sort before BWBR ids), so a cut-off through a tie group would favour some laws
   systematically. pair_id order is stable across runs but unrelated to source.

Usage
-----
  python combine_scores.py --objects data/object_pair_scores.jsonl \\
      --object-config data/object_scores_config.json --norms AI_ACT_norms.json
  python combine_scores.py --objects ... --verbs action_verbs.json
  python combine_scores.py --objects ... --norms ... --w-action 0.3 --w-object 0.7

Reading the output (combined_pair_scores.jsonl):
  pair_id                  stable pair identity (see 5)
  combined                 the pair score (higher = stronger candidate)
  action_score, action_eff raw action score and the value used (after the generic factor)
  action_relation, generic, action_basis, lemma_a, lemma_b
  object_score, object_norm raw and normalised object score
  object_key, object_relation, object_direction, unit_a, unit_b
                           copied from object_pair_scores.jsonl
  `object_direction` (same | a_narrower | b_narrower) is relative to norm_a, norm_b
  and is kept for routing (lex specialis); it does not enter the score.
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
W_ACTION = 0.4
W_OBJECT = 0.6
GENERIC_FACTOR = 0.7
REFERENCE_THRESHOLD = 0.5      # summary only; this script does not select pairs


def o_max(n_norms: int) -> float:
    """Highest object score a pair can reach: a key shared by exactly two norms."""
    if n_norms < 2:
        return 1.0
    return 1 - math.log(3) / math.log(1 + n_norms)


def effective_action(score: float, generic: bool, generic_factor: float) -> float:
    """Discount positive matches on generic verbs; leave 0 and negatives as they are."""
    return score * generic_factor if generic and score > 0 else score


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
    p = argparse.ArgumentParser(description="Combine object and action scores per norm pair.")
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
    p.add_argument("--out", type=Path, default=Path("data/conflict/combined_pair_scores.jsonl"))
    p.add_argument("--w-action", type=float, default=W_ACTION)
    p.add_argument("--w-object", type=float, default=W_OBJECT)
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

        combined = args.w_action * a_eff + args.w_object * o_norm
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
        })

    rows.sort(key=lambda r: (-r["combined"], r["pair_id"]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(args.out, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))

    config = {
        "scheme": "combined = w_action * action_eff + w_object * min(1, object_score / O_MAX)",
        "w_action": args.w_action, "w_object": args.w_object,
        "generic_factor": args.generic_factor,
        "N": n_norms, "O_MAX": round(omax, 4),
        "reference_threshold": args.threshold,
        "pair_id": "sha256('norm_a::norm_b')[:16], norm_a < norm_b",
        "sort": "combined desc, then pair_id",
        "inputs": {"objects": str(args.objects),
                   "actions": str(args.verbs or args.norms),
                   "stage_a": object_config.get("stage_a")},
        "counts": {"object_pairs": len(objects), "scored": len(rows)},
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

    print(f"Scored {len(rows)} pairs -> {args.out}")
    print(f"Pairs from {args.objects} (restricted to Stage A {object_config['stage_a']})")
    print(f"N = {n_norms}, O_MAX = {omax:.4f}, weights: action {args.w_action}, "
          f"object {args.w_object}, generic factor {args.generic_factor}")
    for band in ["< 0", "0.00-0.25", "0.25-0.50", "0.50-0.75", "0.75-1.00"]:
        print(f"  combined {band:>9}: {bands[band]}")
    print(f"At or above reference threshold {args.threshold}: {sum(above.values())} "
          f"(by action relation: {dict(above)})")
    if unknown_ids:
        print(f"Warning: {len(unknown_ids)} norm IDs in the object file have no action profile "
              f"(scored as missing). Check that both stages use the same norm_id, "
              f"e.g. {sorted(unknown_ids)[:3]}", file=sys.stderr)


if __name__ == "__main__":
    main()