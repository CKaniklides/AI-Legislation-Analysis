"""
Action dimension of the norm-pair scorer.

Scores every pair in stageA_pairs.json on how the action verbs of the two norms
relate, producing two independent scores in [0, 1]:

    action_overlap         same verb axis, same polarity      (inform vs inform)
    action_contradiction   same verb axis, opposite polarity  (inform vs not inform,
                                                               retain vs erase)

Verbs come from action_verbs.extract_action_verbs (lexicon families, negation,
clauses). No pair is discarded.

Status handling (per norm):
    verb            scored as below
    none            the action has no recognised verb -> NONE_SCORE on both (0.0 by
                    default: no action means no overlap and no contradiction).
                    Use --none-neutral while the lexicon is still being extended,
                    because "none" also contains verbs the lexicon does not know yet.
    empty           no action text -> NEUTRAL (unknown, not penalised)
    disapplication  exemption; the deontic stage handles it -> NEUTRAL
If either side is "none", the pair gets NONE_SCORE; otherwise if either side is
empty/disapplication, the pair is NEUTRAL.

Scoring when both norms have verbs:
    Every verb contributes literals (axis, polarity) with a weight:
        primary verb            1.0
        other main-clause verb  0.7
        subordinate-clause verb 0.4
    For each pair of literals across the two norms:
        same axis, same polarity      -> overlap evidence       wa * wb
        same axis, opposite polarity  -> contradiction evidence wa * wb
        related axes, same polarity   -> overlap evidence       wa * wb * RELATED_FACTOR
    Evidence on generic axes (ensure, assess, record, use, measure) is multiplied by
    GENERIC_FACTOR: a shared "ensure" proves little. Each score is the maximum evidence
    found, so two primary verbs on the same specific axis score 1.0.
    No shared or related axis -> both 0.0 (unrelated actions).

Negation absorbed into the deontic:
    When a norm is an OBLIGATION or PROHIBITION and its main-clause verb is negated,
    the negation belongs to the deontic, not to the action:
        PROHIBITION + "verstrekt niet"  -> double encoding: prohibited to provide
        OBLIGATION  + "verstrekt niet"  -> really a prohibition to provide
    The verb's polarity is reset to positive so that "must not provide" and
    "prohibited from providing" do not score as opposite actions. Norms of the second
    kind are listed under norms_needing_deontic_flip: the deontic stage should treat
    them as PROHIBITION. Negations in subordinate clauses are part of the action
    content and are kept.

Usage:
    python action_scoring.py --pairs stageA_pairs.json --norms merged_norms.json \
                             --out stageB_action_scores.json
    python action_scoring.py --none-neutral      # "none" scores 0.5 instead of 0.0
    python action_scoring.py --demo
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from action_verbs import extract_action_verbs, load_norm_records
from verb_lexicon import GENERIC_AXES, RELATED_AXES

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

NEUTRAL = 0.5
NONE_SCORE = 0.0          # overridden by --none-neutral

W_PRIMARY = 1.0
W_MAIN = 0.7
W_SUBORDINATE = 0.4

GENERIC_FACTOR = 0.5
RELATED_FACTOR = 0.5

ABSORBING_DEONTICS = {"OBLIGATION", "PROHIBITION"}
NEUTRAL_STATUSES = {"empty", "disapplication"}

# ---------------------------------------------------------------------------
# Per-norm profile
# ---------------------------------------------------------------------------


@dataclass
class Literal:
    axis: str
    polarity: int
    weight: float
    family: str
    matched: str
    role: str          # primary | main | subordinate


@dataclass
class ActionProfile:
    status: str
    literals: list[Literal]
    negation_absorbed: bool
    deontic_flip_needed: bool


def build_profile(extraction: dict, deontic: Any) -> ActionProfile:
    """Turn an extract_action_verbs() result into weighted literals."""
    status = extraction.get("verb_status") or ("verb" if extraction.get("verbs") else "none")
    verbs = extraction.get("verbs") or []
    primary = extraction.get("primary")
    deontic_u = deontic.strip().upper() if isinstance(deontic, str) else None

    absorbed = False
    literals: list[Literal] = []
    for v in verbs:
        is_primary = primary is not None and v["start"] == primary["start"] and v["family"] == primary["family"]
        role = "primary" if is_primary else ("subordinate" if v["subordinate"] else "main")
        weight = {"primary": W_PRIMARY, "main": W_MAIN, "subordinate": W_SUBORDINATE}[role]

        flip_back = (v["negated"] and not v["subordinate"] and deontic_u in ABSORBING_DEONTICS)
        if flip_back:
            absorbed = True
        for a in v["axes"]:
            pol = -a["polarity"] if flip_back else a["polarity"]
            literals.append(Literal(a["axis"], pol, weight, v["family"], v["matched"], role))

    return ActionProfile(
        status=status,
        literals=literals,
        negation_absorbed=absorbed,
        deontic_flip_needed=absorbed and deontic_u == "OBLIGATION",
    )


# ---------------------------------------------------------------------------
# Pair scoring
# ---------------------------------------------------------------------------


@dataclass
class ActionScore:
    action_status_a: str
    action_status_b: str
    action_overlap: float
    action_contradiction: float
    action_known: bool
    action_relation: str          # same | opposite | same+opposite | related | unrelated | no_action | neutral
    overlap_basis: str | None
    contradiction_basis: str | None


def _describe(la: Literal, lb: Literal) -> str:
    sa = "+" if la.polarity > 0 else "-"
    sb = "+" if lb.polarity > 0 else "-"
    return f"{la.family}:{la.axis}{sa} '{la.matched}' ({la.role}) ~ {lb.family}:{lb.axis}{sb} '{lb.matched}' ({lb.role})"


def score_action_pair(pa: ActionProfile, pb: ActionProfile) -> ActionScore:
    if "none" in (pa.status, pb.status):
        return ActionScore(pa.status, pb.status, NONE_SCORE, NONE_SCORE, False,
                           "no_action", None, None)
    if pa.status in NEUTRAL_STATUSES or pb.status in NEUTRAL_STATUSES:
        return ActionScore(pa.status, pb.status, NEUTRAL, NEUTRAL, False, "neutral", None, None)

    best_ov, best_co = 0.0, 0.0
    basis_ov: str | None = None
    basis_co: str | None = None
    same_axis_ov = False

    for la in pa.literals:
        for lb in pb.literals:
            w = la.weight * lb.weight
            if la.axis == lb.axis:
                if la.axis in GENERIC_AXES:
                    w *= GENERIC_FACTOR
                if la.polarity == lb.polarity:
                    if w > best_ov:
                        best_ov, basis_ov, same_axis_ov = w, _describe(la, lb), True
                else:
                    if w > best_co:
                        best_co, basis_co = w, _describe(la, lb)
            elif la.polarity == lb.polarity and frozenset((la.axis, lb.axis)) in RELATED_AXES:
                w *= RELATED_FACTOR
                if la.axis in GENERIC_AXES or lb.axis in GENERIC_AXES:
                    w *= GENERIC_FACTOR
                if w > best_ov:
                    best_ov, basis_ov, same_axis_ov = w, _describe(la, lb) + " [related]", False

    if best_ov and best_co:
        relation = "same+opposite"
    elif best_co:
        relation = "opposite"
    elif best_ov:
        relation = "same" if same_axis_ov else "related"
    else:
        relation = "unrelated"

    return ActionScore(pa.status, pb.status, round(best_ov, 4), round(best_co, 4), True,
                       relation, basis_ov, basis_co)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def _inner(rec: dict) -> dict:
    return rec["norm"] if isinstance(rec.get("norm"), dict) else rec


def build_profiles_from_norms(norms_path: Path) -> dict[str, tuple[ActionProfile, Any]]:
    profiles: dict[str, tuple[ActionProfile, Any]] = {}
    for rec in load_norm_records(norms_path):
        norm = _inner(rec)
        ext = extract_action_verbs(norm.get("action"))
        profiles[rec.get("norm_id")] = (build_profile(ext, norm.get("deontic")), norm.get("deontic"))
    return profiles


def build_profiles_from_verbs(verbs_path: Path) -> dict[str, tuple[ActionProfile, Any]]:
    """Reuse an action_verbs.py output file instead of re-extracting."""
    with verbs_path.open(encoding="utf-8") as f:
        data = json.load(f)
    return {r["norm_id"]: (build_profile(r, r.get("deontic")), r.get("deontic"))
            for r in data["norms"]}


def load_pairs(path: Path) -> tuple[list[str], list[tuple[int, int]], dict]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    pairs = [(int(i), int(j)) for i, j in data["pairs"]]
    meta = {k: v for k, v in data.items() if k not in ("norm_ids", "pairs")}
    return data["norm_ids"], pairs, meta


def run(pairs_path: Path, norms_path: Path | None, verbs_path: Path | None, out_path: Path) -> None:
    norm_ids, pairs, stage_a_meta = load_pairs(pairs_path)
    if verbs_path:
        profiles = build_profiles_from_verbs(verbs_path)
        source = str(verbs_path)
    else:
        profiles = build_profiles_from_norms(norms_path)
        source = str(norms_path)

    missing_profile = ActionProfile("empty", [], False, False)
    missing_ids: set[str] = set()
    relation_counts: Counter[str] = Counter()
    scores: list[dict] = []

    for i, j in pairs:
        id_a, id_b = norm_ids[i], norm_ids[j]
        pa = profiles.get(id_a, (None, None))[0]
        pb = profiles.get(id_b, (None, None))[0]
        if pa is None:
            missing_ids.add(id_a)
            pa = missing_profile
        if pb is None:
            missing_ids.add(id_b)
            pb = missing_profile
        res = score_action_pair(pa, pb)
        relation_counts[res.action_relation] += 1
        scores.append({"i": i, "j": j, "norm_id_a": id_a, "norm_id_b": id_b, **asdict(res)})

    status_counts = Counter(p.status for p, _ in profiles.values())
    absorbed = sorted(nid for nid, (p, _) in profiles.items() if p.negation_absorbed)
    flip = sorted(nid for nid, (p, _) in profiles.items() if p.deontic_flip_needed)

    stats = {
        "n_pairs": len(pairs),
        "n_norms": len(profiles),
        "norm_status": dict(status_counts.most_common()),
        "pair_relation": dict(relation_counts.most_common()),
        "norms_negation_absorbed": absorbed,
        "norms_needing_deontic_flip": flip,
        "norm_ids_missing": sorted(missing_ids),
    }
    output = {
        "stage": "B-action",
        "input_pairs": str(pairs_path),
        "input_verbs": source,
        "stage_a_rule": stage_a_meta.get("rule"),
        "parameters": {
            "neutral": NEUTRAL, "none_score": NONE_SCORE,
            "w_primary": W_PRIMARY, "w_main": W_MAIN, "w_subordinate": W_SUBORDINATE,
            "generic_factor": GENERIC_FACTOR, "related_factor": RELATED_FACTOR,
        },
        "stats": stats,
        "scores": scores,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)

    print(f"Scored {len(pairs)} pairs -> {out_path}")
    print("Norm status:", stats["norm_status"])
    print("Pair relation:", stats["pair_relation"])
    print(f"Negation absorbed into deontic: {len(absorbed)} norms "
          f"({len(flip)} OBLIGATION norms that are really prohibitions)")
    if missing_ids:
        print(f"warning: {len(missing_ids)} norm_ids not found in the norms file", file=sys.stderr)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

DEMO_PAIRS = [
    (("OBLIGATION", "stelt het AI-bureau in kennis van de beëindiging"),
     ("OBLIGATION", "meldt de beëindiging aan het AI-bureau")),
    (("OBLIGATION", "bewaart de technische documentatie"),
     ("OBLIGATION", "wist de technische documentatie")),
    (("OBLIGATION", "verstrekt de gegevens niet aan derden"),
     ("PROHIBITION", "verstrekt de gegevens aan derden")),
    (("PERMISSION", "verstrekt de gegevens niet aan derden"),
     ("OBLIGATION", "verstrekt de gegevens aan derden")),
    (("OBLIGATION", "verstrekt de documentatie aan het AI-bureau"),
     ("OBLIGATION", "maakt de documentatie openbaar")),
    (("OBLIGATION", "waarborgt de cyberbeveiliging"),
     ("OBLIGATION", "waarborgt de nauwkeurigheid")),
    (("OBLIGATION", "schrijft voor dat de aangemelde instantie certificaten intrekt"),
     ("COMPETENCE", "keurt het verzoek van de aangemelde instantie goed")),
    (("OBLIGATION", "beëindigt het mandaat"),
     ("OBLIGATION", "verstrekt de documentatie")),
    (("OBLIGATION", "zijn relevant en representatief"),
     ("OBLIGATION", "verstrekt de documentatie")),
    (("PROHIBITION", "De in dit artikel genoemde verplichting geldt niet"),
     ("OBLIGATION", "wijst een gemachtigde aan")),
]


def demo() -> None:
    for (da, aa), (db, ab) in DEMO_PAIRS:
        pa = build_profile(extract_action_verbs(aa), da)
        pb = build_profile(extract_action_verbs(ab), db)
        s = score_action_pair(pa, pb)
        print(f"\n[{da}] {aa}\n[{db}] {ab}")
        print(f"   overlap={s.action_overlap}  contradiction={s.action_contradiction}  "
              f"relation={s.action_relation}")
        if s.overlap_basis:
            print(f"   overlap basis:       {s.overlap_basis}")
        if s.contradiction_basis:
            print(f"   contradiction basis: {s.contradiction_basis}")
        for label, p in (("a", pa), ("b", pb)):
            if p.negation_absorbed:
                print(f"   norm {label}: negation absorbed into deontic"
                      + (" (treat as PROHIBITION)" if p.deontic_flip_needed else ""))


def main() -> None:
    global NONE_SCORE
    parser = argparse.ArgumentParser(description="Score norm pairs on action verbs.")
    parser.add_argument("--pairs", type=Path, default=Path("stageA_pairs.json"))
    parser.add_argument("--norms", type=Path, default=Path("merged_norms.json"))
    parser.add_argument("--verbs", type=Path, default=None,
                        help="reuse an action_verbs.py output instead of extracting from --norms")
    parser.add_argument("--out", type=Path, default=Path("stageB_action_scores.json"))
    parser.add_argument("--none-neutral", action="store_true",
                        help="score norms without a recognised verb as neutral (0.5) instead of 0.0")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    if args.none_neutral:
        NONE_SCORE = NEUTRAL
    if args.demo:
        demo()
        return
    run(args.pairs, None if args.verbs else args.norms, args.verbs, args.out)


if __name__ == "__main__":
    main()