"""
Combine the per-dimension scores and list the highest-scoring pairs for
contradiction and duplication.

Combined scores: geometric mean of the three dimensions (equal weights), taking the
stronger of two routes. Range 0..1, all-neutral pair = 0.5, and any dimension at 0
gives 0 for that route:

    duplication   = max( gm(actor, deontic_overlap,       action_overlap),
                         gm(actor, deontic_contradiction, action_contradiction) )
    contradiction = max( gm(actor, deontic_contradiction, action_overlap),
                         gm(actor, deontic_overlap,       action_contradiction) )
    gm(x, y, z)   = (x * y * z) ** (1/3)

Why a geometric mean and not an average: the dimensions are jointly necessary. With an
average, two identical obligations would still score 0.5 on contradiction, carried by the
perfect action match alone.

Why two routes: a contradiction is EITHER conflicting deontics on the same action
("must provide X" vs "prohibited from providing X") OR the same deontic on opposite
actions ("must retain logs" vs "must erase logs"). Likewise a duplicate can be phrased
with opposite deontic AND opposite action ("must retain" vs "prohibited from erasing").
A plain average would score the first example LOW on contradiction, because its
action_contradiction is 0.

The actor score is optional (--actor); without it every pair gets actor = 0.5 (neutral)
and the maximum combined score is 0.5 ** (1/3) = 0.79.

Filters:
    --top N                     pairs per list (default 100)
    --min-known K               dimensions that must be known (default 1). A dimension is
                                unknown when it fell back to neutral (missing deontic,
                                empty action or disapplication, no actor score).
    --keep-rule-exceptions      rule-exception pairs (exemption vs deontic norm) are left
                                out of the contradiction list by default.

Usage:
    python rank_pairs.py --deontic stageB_deontic_scores.json \
                         --action stageB_action_scores.json \
                         --norms merged_norms.json --top 100
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

NEUTRAL = 0.5


def _load_scores(path: Path) -> dict[tuple[int, int], dict]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return {(int(r["i"]), int(r["j"])): r for r in data["scores"]}


def _load_texts(path: Path | None) -> dict[str, dict]:
    if not path:
        return {}
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    records = data["norms"] if isinstance(data, dict) and "norms" in data else data
    out = {}
    for rec in records:
        norm = rec["norm"] if isinstance(rec.get("norm"), dict) else rec
        out[rec.get("norm_id")] = {"addressee": norm.get("addressee"), "action": norm.get("action")}
    return out


def gm(*xs: float) -> float:
    prod = 1.0
    for x in xs:
        prod *= max(float(x), 0.0)
    return prod ** (1.0 / len(xs))


def combine(actor: float, d_ov: float, d_co: float, a_ov: float, a_co: float) -> dict:
    dup_direct, dup_mirror = gm(actor, d_ov, a_ov), gm(actor, d_co, a_co)
    con_deontic, con_action = gm(actor, d_co, a_ov), gm(actor, d_ov, a_co)
    return {
        "duplication": round(max(dup_direct, dup_mirror), 4),
        "duplication_route": "same deontic, same action" if dup_direct >= dup_mirror
                             else "opposite deontic, opposite action",
        "contradiction": round(max(con_deontic, con_action), 4),
        "contradiction_route": "conflicting deontic, same action" if con_deontic >= con_action
                               else "same deontic, opposite action",
    }


def run(deontic_path: Path, action_path: Path, actor_path: Path | None, norms_path: Path | None,
        out_dir: Path, top: int, min_known: int, keep_rule_exceptions: bool) -> None:
    deontic = _load_scores(deontic_path)
    action = _load_scores(action_path)
    actor = _load_scores(actor_path) if actor_path else {}
    texts = _load_texts(norms_path)

    rows: list[dict] = []
    for key, a in action.items():
        d = deontic.get(key, {})
        ac = actor.get(key, {})
        actor_score = ac.get("actor_similarity", NEUTRAL)
        d_ov = d.get("deontic_overlap", NEUTRAL)
        d_co = d.get("deontic_contradiction", NEUTRAL)
        a_ov = a.get("action_overlap", NEUTRAL)
        a_co = a.get("action_contradiction", NEUTRAL)

        known = (int(bool(d.get("deontic_known", False)))
                 + int(a.get("action_relation") != "neutral")
                 + int(bool(ac.get("actor_known", "actor_similarity" in ac))))

        rows.append({
            "norm_id_a": a["norm_id_a"], "norm_id_b": a["norm_id_b"],
            **combine(actor_score, d_ov, d_co, a_ov, a_co),
            "known_dimensions": known,
            "actor": actor_score,
            "deontic_a": d.get("deontic_a"), "deontic_b": d.get("deontic_b"),
            "deontic_overlap": d_ov, "deontic_contradiction": d_co,
            "rule_exception": bool(d.get("rule_exception", False)),
            "action_relation": a.get("action_relation"),
            "action_overlap": a_ov, "action_contradiction": a_co,
            "overlap_basis": a.get("overlap_basis"),
            "contradiction_basis": a.get("contradiction_basis"),
            "addressee_a": texts.get(a["norm_id_a"], {}).get("addressee"),
            "action_a": texts.get(a["norm_id_a"], {}).get("action"),
            "addressee_b": texts.get(a["norm_id_b"], {}).get("addressee"),
            "action_b": texts.get(a["norm_id_b"], {}).get("action"),
        })

    eligible = [r for r in rows if r["known_dimensions"] >= min_known]
    contra_pool = eligible if keep_rule_exceptions else [r for r in eligible if not r["rule_exception"]]

    top_contra = sorted(contra_pool, key=lambda r: -r["contradiction"])[:top]
    top_dup = sorted(eligible, key=lambda r: -r["duplication"])[:top]

    out_dir.mkdir(parents=True, exist_ok=True)
    common = ["norm_id_a", "norm_id_b", "known_dimensions", "actor",
              "deontic_a", "deontic_b", "deontic_overlap", "deontic_contradiction", "rule_exception",
              "action_relation", "action_overlap", "action_contradiction",
              "addressee_a", "action_a", "addressee_b", "action_b"]
    lists = [
        ("top_contradictions.csv", top_contra,
         ["rank", "contradiction", "contradiction_route"] + common + ["contradiction_basis"]),
        ("top_duplications.csv", top_dup,
         ["rank", "duplication", "duplication_route"] + common + ["overlap_basis"]),
    ]
    for name, items, fields in lists:
        with (out_dir / name).open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, delimiter=";", extrasaction="ignore")
            w.writeheader()
            for rank, r in enumerate(items, 1):
                w.writerow({"rank": rank, **r})

    def summary(label: str, key: str, pool: list[dict]) -> None:
        vals = sorted((r[key] for r in pool), reverse=True)
        if not vals:
            print(f"{label}: no pairs")
            return
        pick = lambda q: vals[min(int(q * len(vals)), len(vals) - 1)]
        print(f"{label}: max={vals[0]:.3f}  top1%={pick(0.01):.3f}  top5%={pick(0.05):.3f}  "
              f"median={pick(0.5):.3f}  min={vals[-1]:.3f}")

    print(f"Pairs: {len(rows)}   eligible (>= {min_known} known dimensions): {len(eligible)}")
    if not actor_path:
        print("No actor scores: actor = 0.5 for every pair, so the maximum combined score is 0.79.")
    summary("Contradiction", "contradiction", contra_pool)
    summary("Duplication  ", "duplication", eligible)
    print(f"Top {top} written to {out_dir / 'top_contradictions.csv'} and {out_dir / 'top_duplications.csv'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Rank norm pairs by contradiction and duplication.")
    parser.add_argument("--deontic", type=Path, default=Path("stageB_deontic_scores.json"))
    parser.add_argument("--action", type=Path, default=Path("stageB_action_scores.json"))
    parser.add_argument("--actor", type=Path, default=None,
                        help="optional actor scores (fields actor_similarity, actor_known)")
    parser.add_argument("--norms", type=Path, default=None,
                        help="optional merged_norms.json, to add addressee and action text to the CSVs")
    parser.add_argument("--out-dir", type=Path, default=Path("."))
    parser.add_argument("--top", type=int, default=100)
    parser.add_argument("--min-known", type=int, default=1)
    parser.add_argument("--keep-rule-exceptions", action="store_true")
    args = parser.parse_args()
    run(args.deontic, args.action, args.actor, args.norms, args.out_dir,
        args.top, args.min_known, args.keep_rule_exceptions)


if __name__ == "__main__":
    main()