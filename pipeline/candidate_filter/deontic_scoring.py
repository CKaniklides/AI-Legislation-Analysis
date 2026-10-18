"""
Deontic dimension of the norm-pair scorer.

Scores every pair in stageA_pairs.json on how its two deontic labels relate,
producing ONE score in [0, 1]:

    deontic_score   how strongly the deontic combination suggests that the pair is a
                    candidate for review, i.e. a duplication OR a contradiction.

Duplication and contradiction are deliberately not distinguished: both score high.
"must X" vs "must X" (duplicate) and "must X" vs "must not X" (conflict) are equally
interesting at this stage; the final LLM review decides which one it is. A side
effect is that action polarity no longer matters here: "obliged to X" vs
"prohibited from not-X" scores high either way, which is what we want.

Deontic labels considered (as emitted by the extractor):
    OBLIGATION, PROHIBITION, PERMISSION, EXEMPTION, DELEGATION, COMPETENCE, RIGHT

No pair is discarded. Missing or unrecognised labels score NEUTRAL (0.5) and are
flagged with deontic_known = False, so they are not penalised but stay visible.

Exemptions: a norm whose action switches another norm off ("geldt niet",
"is niet van toepassing") is relabelled EXEMPTION, whatever the extractor called it
(in practice these come out as PROHIBITION). An exemption against any other label
is a rule-exception relation, normally intended by the legislator: it scores low
and is flagged rule_exception = True. The original label is kept in
deontic_a_raw/_b_raw. Detection is shared with action_verbs.py (is_disapplication),
so both stages agree.

Usage:
    python deontic_scoring.py
    python deontic_scoring.py --pairs stageA_pairs.json --norms merged_norms.json \
                              --out stageB_deontic_scores.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from action_verbs import is_disapplication

# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

Deontic = Literal[
    "OBLIGATION", "PROHIBITION", "PERMISSION", "EXEMPTION", "DELEGATION",
    "COMPETENCE", "RIGHT",
]

OBL = "OBLIGATION"
PRO = "PROHIBITION"
PER = "PERMISSION"
EXE = "EXEMPTION"
DEL = "DELEGATION"
COM = "COMPETENCE"
RIG = "RIGHT"

KNOWN_LABELS: tuple[str, ...] = get_args(Deontic)

NEUTRAL = 0.5

# Spelling variants -> canonical label. The extractor is constrained to the Literal
# above, so this only guards against case/whitespace drift and occasional Dutch output.
ALIASES: dict[str, str] = {
    "OBLIGATION": OBL, "VERPLICHTING": OBL,
    "PROHIBITION": PRO, "VERBOD": PRO,
    "PERMISSION": PER, "TOESTEMMING": PER,
    "EXEMPTION": EXE, "VRIJSTELLING": EXE, "UITZONDERING": EXE,
    "DELEGATION": DEL, "DELEGATIE": DEL,
    "COMPETENCE": COM, "BEVOEGDHEID": COM,
    "RIGHT": RIG, "RECHT": RIG,
}

# ---------------------------------------------------------------------------
# Deontic table
# ---------------------------------------------------------------------------
# (label_a, label_b) -> (score, rationale)
# Only one order per combination is written here; the reverse is filled in
# automatically. Values assume the two norms concern the same action -- the
# action and object dimensions decide whether that is true.
#
# Rough scale:
#   1.0   same label (duplication) or direct opposition (contradiction)
#   0.7+  strong relation: subsumption, correlatives, competing powers
#   0.4   weak, category-crossing relation
#   0.2-0.3  rule-exception or barely related

_TABLE_HALF: dict[tuple[str, str], tuple[float, str]] = {
    # Same label: duplication candidates.
    (OBL, OBL): (1.00, "same deontic: duplication candidate"),
    (PRO, PRO): (1.00, "same deontic: duplication candidate"),
    (PER, PER): (1.00, "same deontic: duplication candidate"),
    (COM, COM): (1.00, "same deontic: duplicate or competing competence"),
    (DEL, DEL): (1.00, "same deontic: duplicate or competing delegation"),
    (RIG, RIG): (1.00, "same deontic: duplication candidate"),
    (EXE, EXE): (0.80, "two exemptions: possible duplicate exemption"),

    # Direct opposition: contradiction candidates.
    (OBL, PRO): (1.00, "must vs must not: core contradiction"),
    (PRO, PER): (1.00, "may vs must not: core contradiction"),
    (PRO, RIG): (0.90, "right to X vs prohibition of X: contradiction"),

    # Strong relations.
    (OBL, PER): (0.75, "obligation entails permission: subsumption overlap"),
    (PER, RIG): (0.75, "permission vs right: overlapping entitlements"),
    (OBL, RIG): (0.60, "right vs duty: possible correlatives of one legal relation"),
    (COM, DEL): (0.80, "competence vs delegation of competence: overlap or allocation conflict"),

    # Competence is a power, not a duty: weaker, category-crossing relations.
    (COM, PRO): (0.50, "power to do what is prohibited: possible conflict"),
    (COM, PER): (0.50, "power vs permission: related enabling norms"),
    (COM, OBL): (0.40, "power vs duty: different categories, weak relation"),
    (COM, RIG): (0.40, "power vs right: different categories, weak relation"),

    # Delegation transfers powers; against first-order norms the relation is weak.
    (DEL, PRO): (0.30, "delegation vs prohibition: weak relation"),
    (DEL, OBL): (0.30, "delegation vs duty: weak relation"),
    (DEL, PER): (0.30, "delegation vs permission: weak relation"),
    (DEL, RIG): (0.20, "delegation vs right: barely related"),

    # Exemptions switch other norms off: rule-exception, normally intended.
    (EXE, OBL): (0.30, "rule-exception: exemption from a duty"),
    (EXE, PRO): (0.30, "rule-exception: exemption from a prohibition"),
    (EXE, PER): (0.30, "rule-exception: exemption vs permission"),
    (EXE, COM): (0.30, "rule-exception: exemption vs competence"),
    (EXE, DEL): (0.30, "rule-exception: exemption vs delegation"),
    (EXE, RIG): (0.30, "rule-exception: exemption vs right"),
}

# Combinations reported with rule_exception = True.
RULE_EXCEPTION_PAIRS = {frozenset((EXE, x)) for x in KNOWN_LABELS if x != EXE}


def _build_table() -> dict[tuple[str, str], tuple[float, str]]:
    table: dict[tuple[str, str], tuple[float, str]] = {}
    for (a, b), value in _TABLE_HALF.items():
        for key in ((a, b), (b, a)):
            if key in table and table[key] != value:
                raise ValueError(f"Conflicting table entries for {key}")
            table[key] = value
    missing = [(a, b) for a in KNOWN_LABELS for b in KNOWN_LABELS if (a, b) not in table]
    if missing:
        raise ValueError(f"Deontic table is incomplete, missing: {missing}")
    unknown = {lab for key in table for lab in key} - set(KNOWN_LABELS)
    if unknown:
        raise ValueError(f"Deontic table uses unknown labels: {unknown}")
    for key, (score, _) in table.items():
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"Score out of range for {key}")
    return table


DEONTIC_TABLE = _build_table()

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass
class DeonticScore:
    deontic_a: str | None
    deontic_b: str | None
    deontic_a_raw: Any
    deontic_b_raw: Any
    deontic_score: float
    deontic_known: bool
    rule_exception: bool
    deontic_rationale: str


def normalize_deontic(raw: Any) -> str | None:
    """Map a raw deontic value to a canonical label, or None if unrecognised."""
    if not isinstance(raw, str):
        return None
    key = raw.strip().upper().replace("-", "_").replace(" ", "_")
    if not key:
        return None
    return ALIASES.get(key)


def _get_field(record: dict | None, field: str) -> Any:
    """Accept either a merged_norms record ({'norm': {...}}) or a bare norm dict."""
    if not isinstance(record, dict):
        return None
    inner = record.get("norm")
    if isinstance(inner, dict):
        return inner.get(field)
    return record.get(field)


def effective_deontic(record: dict | None) -> tuple[str | None, Any, bool]:
    """Return (canonical label, raw label, relabelled?).
    A disapplying action overrides the extracted label with EXEMPTION."""
    raw = _get_field(record, "deontic")
    if is_disapplication(_get_field(record, "action")):
        return EXE, raw, normalize_deontic(raw) != EXE
    return normalize_deontic(raw), raw, False


def score_deontic_pair(norm_a: dict | None, norm_b: dict | None) -> DeonticScore:
    """Score a pair of norms on their (effective) deontic labels alone."""
    label_a, raw_a, _ = effective_deontic(norm_a)
    label_b, raw_b, _ = effective_deontic(norm_b)

    if label_a is None or label_b is None:
        return DeonticScore(
            deontic_a=label_a,
            deontic_b=label_b,
            deontic_a_raw=raw_a,
            deontic_b_raw=raw_b,
            deontic_score=NEUTRAL,
            deontic_known=False,
            rule_exception=False,
            deontic_rationale="missing or unrecognised deontic: neutral",
        )

    score, rationale = DEONTIC_TABLE[(label_a, label_b)]
    return DeonticScore(
        deontic_a=label_a,
        deontic_b=label_b,
        deontic_a_raw=raw_a,
        deontic_b_raw=raw_b,
        deontic_score=score,
        deontic_known=True,
        rule_exception=frozenset((label_a, label_b)) in RULE_EXCEPTION_PAIRS,
        deontic_rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_norms(path: Path) -> dict[str, dict]:
    """Load merged_norms.json and index records by norm_id.

    Accepts a top-level list of records, a dict with a 'norms' list, or a dict
    already keyed by norm_id.
    """
    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        records = data
    elif isinstance(data, dict) and isinstance(data.get("norms"), list):
        records = data["norms"]
    elif isinstance(data, dict):
        return {k: v for k, v in data.items() if isinstance(v, dict)}
    else:
        raise ValueError(f"Unrecognised structure in {path}")

    index: dict[str, dict] = {}
    duplicates = 0
    for rec in records:
        if not isinstance(rec, dict) or "norm_id" not in rec:
            continue
        if rec["norm_id"] in index:
            duplicates += 1
        index[rec["norm_id"]] = rec
    if duplicates:
        print(f"warning: {duplicates} duplicate norm_ids in {path}; last one kept",
              file=sys.stderr)
    return index


def load_pairs(path: Path) -> tuple[list[str], list[tuple[int, int]], dict]:
    """Load stageA_pairs.json -> (norm_ids, index pairs, metadata)."""
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    norm_ids = data["norm_ids"]
    pairs = [(int(i), int(j)) for i, j in data["pairs"]]
    meta = {k: v for k, v in data.items() if k not in ("norm_ids", "pairs")}
    return norm_ids, pairs, meta


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(pairs_path: Path, norms_path: Path, out_path: Path) -> None:
    norm_ids, pairs, stage_a_meta = load_pairs(pairs_path)
    norms = load_norms(norms_path)

    label_counts: Counter[str] = Counter()
    combo_counts: Counter[str] = Counter()
    score_counts: Counter[float] = Counter()
    unrecognised_raw: Counter[str] = Counter()
    missing_norm_ids: set[str] = set()
    scores: list[dict] = []

    for i, j in pairs:
        id_a, id_b = norm_ids[i], norm_ids[j]
        rec_a, rec_b = norms.get(id_a), norms.get(id_b)
        for nid, rec in ((id_a, rec_a), (id_b, rec_b)):
            if rec is None:
                missing_norm_ids.add(nid)
            else:
                label, raw, _ = effective_deontic(rec)
                if label is None:
                    unrecognised_raw[str(raw)] += 1

        result = score_deontic_pair(rec_a, rec_b)
        combo_counts[" / ".join(sorted([str(result.deontic_a), str(result.deontic_b)]))] += 1
        score_counts[result.deontic_score] += 1

        scores.append({"i": i, "j": j, "norm_id_a": id_a, "norm_id_b": id_b, **asdict(result)})

    relabelled: Counter[str] = Counter()
    for rec in norms.values():
        label, raw, was_relabelled = effective_deontic(rec)
        label_counts[str(label)] += 1
        if was_relabelled:
            relabelled[f"{raw} -> {label}"] += 1

    stats = {
        "n_pairs": len(pairs),
        "n_norms_loaded": len(norms),
        "pairs_with_unknown_deontic": sum(1 for s in scores if not s["deontic_known"]),
        "pairs_rule_exception": sum(1 for s in scores if s["rule_exception"]),
        "mean_deontic_score": (sum(s["deontic_score"] for s in scores) / len(scores)) if scores else None,
        "score_distribution": {str(k): v for k, v in sorted(score_counts.items(), reverse=True)},
        "norms_relabelled": dict(relabelled.most_common()),
        "norm_labels": dict(label_counts.most_common()),
        "pair_combinations": dict(combo_counts.most_common()),
        "unrecognised_raw_labels": dict(unrecognised_raw.most_common()),
        "norm_ids_missing_from_norms_file": sorted(missing_norm_ids),
    }

    output = {
        "stage": "B-deontic",
        "input_pairs": str(pairs_path),
        "input_norms": str(norms_path),
        "stage_a_rule": stage_a_meta.get("rule"),
        "neutral_value": NEUTRAL,
        "labels": list(KNOWN_LABELS),
        "table": [
            {"a": a, "b": b, "score": s, "rationale": r}
            for (a, b), (s, r) in _TABLE_HALF.items()
        ],
        "stats": stats,
        "scores": scores,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)

    print(f"Scored {stats['n_pairs']} pairs -> {out_path}")
    print(f"Mean deontic score: {stats['mean_deontic_score']}")
    print("Score distribution:", stats["score_distribution"])
    print(f"Pairs with unknown deontic: {stats['pairs_with_unknown_deontic']}")
    print("Norm labels (after relabelling):", stats["norm_labels"])
    print("Relabelled norms:", stats["norms_relabelled"])
    print(f"Rule-exception pairs: {stats['pairs_rule_exception']}")
    if unrecognised_raw:
        print("Unrecognised raw labels:", dict(unrecognised_raw))
    if missing_norm_ids:
        print(f"warning: {len(missing_norm_ids)} norm_ids from the pairs file "
              f"were not found in the norms file", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Score norm pairs on deontic labels.")
    parser.add_argument("--pairs", type=Path, default=Path("stageA_pairs.json"))
    parser.add_argument("--norms", type=Path, default=Path("merged_norms.json"))
    parser.add_argument("--out", type=Path, default=Path("stageB_deontic_scores.json"))
    args = parser.parse_args()
    run(args.pairs, args.norms, args.out)


if __name__ == "__main__":
    main()