"""
Deontic dimension of the norm-pair scorer.

Scores every pair in stageA_pairs.json on how its two deontic labels relate,
producing two independent scores in [0, 1]:

    deontic_overlap        how much the deontic combination suggests duplication
    deontic_contradiction  how much the deontic combination suggests conflict

No pair is discarded. Missing or unrecognised labels score NEUTRAL (0.5) on both
and are flagged with deontic_known = False, so they are not penalised but stay
visible.

Exemptions: a norm whose action switches another norm off ("geldt niet",
"is niet van toepassing") is relabelled EXEMPTION, whatever the extractor called it
(in practice these come out as PROHIBITION). An exemption against a deontic norm
is a rule-exception relation, not a contradiction: it scores low on both and is
flagged rule_exception = True. The original label is kept in deontic_a_raw/_b_raw.
Detection is shared with action_verbs.py (is_disapplication), so both stages agree.

Caveat: this dimension looks at the deontic labels ONLY. It cannot see action
polarity, so "obliged to X" vs "prohibited from not-X" (a duplicate) will score
as a contradiction here. That has to be corrected by negation normalisation
upstream, or by the action dimension when the scores are combined.

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
from typing import Any

from action_verbs import is_disapplication

# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

OBL = "OBLIGATION"
PRO = "PROHIBITION"
PER = "PERMISSION"
COM = "COMPETENCE"
DEF = "DEFINITION"
EXE = "EXEMPTION"

KNOWN_LABELS = (OBL, PRO, PER, COM, DEF, EXE)

NEUTRAL = 0.5

# Spelling variants -> canonical label. Extend if the extractor emits others.
ALIASES: dict[str, str] = {
    "OBLIGATION": OBL, "OBLIGATORY": OBL, "DUTY": OBL, "VERPLICHTING": OBL,
    "PROHIBITION": PRO, "PROHIBITED": PRO, "FORBIDDEN": PRO, "VERBOD": PRO,
    "PERMISSION": PER, "PERMITTED": PER, "TOESTEMMING": PER,
    "COMPETENCE": COM, "POWER": COM, "BEVOEGDHEID": COM,
    "DEFINITION": DEF, "DEFINITIE": DEF,
    "EXEMPTION": EXE, "EXCEPTION": EXE, "VRIJSTELLING": EXE, "UITZONDERING": EXE,
}

# ---------------------------------------------------------------------------
# Deontic table
# ---------------------------------------------------------------------------
# (label_a, label_b) -> (overlap, contradiction, rationale)
# Only one order per combination is written here; the reverse is filled in
# automatically. Values assume the two norms concern the same action with the
# same polarity -- the action dimension decides whether that is true.

_TABLE_HALF: dict[tuple[str, str], tuple[float, float, str]] = {
    # Same deontic: classic duplication, no conflict.
    (OBL, OBL): (1.00, 0.00, "same deontic: duplication candidate"),
    (PRO, PRO): (1.00, 0.00, "same deontic: duplication candidate"),
    (PER, PER): (1.00, 0.00, "same deontic: duplication candidate"),
    (COM, COM): (1.00, 0.00, "same deontic: duplication candidate"),

    # Core conflicts.
    (OBL, PRO): (0.10, 1.00, "must vs must not: core contradiction"),
    (PRO, PER): (0.10, 1.00, "may vs must not: core contradiction"),

    # Subsumption: an obligation entails the permission.
    (OBL, PER): (0.75, 0.10, "obligation entails permission: subsumption overlap"),

    # Competence is a power, not a duty: weaker, category-crossing relations.
    (COM, OBL): (0.40, 0.20, "power vs duty: different categories, weak relation"),
    (COM, PER): (0.50, 0.10, "power vs permission: related enabling norms"),
    (COM, PRO): (0.20, 0.50, "power to do what is prohibited: weak conflict"),

    # Definitions are not deontic. Two definitions may duplicate or compete;
    # a definition vs any deontic norm is neutral.
    (DEF, DEF): (0.80, 0.50, "two definitions: possible duplicate or competing definition"),
    (DEF, OBL): (NEUTRAL, NEUTRAL, "definition vs deontic norm: neutral"),
    (DEF, PRO): (NEUTRAL, NEUTRAL, "definition vs deontic norm: neutral"),
    (DEF, PER): (NEUTRAL, NEUTRAL, "definition vs deontic norm: neutral"),
    (DEF, COM): (NEUTRAL, NEUTRAL, "definition vs deontic norm: neutral"),

    # Exemptions switch other norms off. Against a deontic norm that is a rule-exception
    # relation: related, but neither a duplicate nor a conflict. Two exemptions may
    # duplicate each other.
    (EXE, EXE): (0.80, 0.20, "two exemptions: possible duplicate exemption"),
    (EXE, OBL): (0.30, 0.30, "rule-exception: exemption from a duty, not a conflict"),
    (EXE, PRO): (0.30, 0.30, "rule-exception: exemption from a prohibition, not a conflict"),
    (EXE, PER): (0.30, 0.30, "rule-exception: exemption vs permission, weak relation"),
    (EXE, COM): (0.30, 0.30, "rule-exception: exemption vs competence, weak relation"),
    (EXE, DEF): (NEUTRAL, NEUTRAL, "definition vs exemption: neutral"),
}

# Combinations reported with rule_exception = True.
RULE_EXCEPTION_PAIRS = {frozenset((EXE, x)) for x in (OBL, PRO, PER, COM)}


def _build_table() -> dict[tuple[str, str], tuple[float, float, str]]:
    table: dict[tuple[str, str], tuple[float, float, str]] = {}
    for (a, b), value in _TABLE_HALF.items():
        table[(a, b)] = value
        table[(b, a)] = value
    missing = [(a, b) for a in KNOWN_LABELS for b in KNOWN_LABELS if (a, b) not in table]
    if missing:
        raise ValueError(f"Deontic table is incomplete, missing: {missing}")
    for (a, b), (ov, co, _) in table.items():
        if not (0.0 <= ov <= 1.0 and 0.0 <= co <= 1.0):
            raise ValueError(f"Score out of range for {(a, b)}")
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
    deontic_overlap: float
    deontic_contradiction: float
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


def _get_deontic(record: dict | None) -> Any:
    """Accept either a merged_norms record ({'norm': {...}}) or a bare norm dict."""
    if not isinstance(record, dict):
        return None
    inner = record.get("norm")
    if isinstance(inner, dict):
        return inner.get("deontic")
    return record.get("deontic")


def _get_action(record: dict | None) -> Any:
    if not isinstance(record, dict):
        return None
    inner = record.get("norm")
    if isinstance(inner, dict):
        return inner.get("action")
    return record.get("action")


def effective_deontic(record: dict | None) -> tuple[str | None, Any, bool]:
    """Return (canonical label, raw label, relabelled?).
    A disapplying action overrides the extracted label with EXEMPTION."""
    raw = _get_deontic(record)
    if is_disapplication(_get_action(record)):
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
            deontic_overlap=NEUTRAL,
            deontic_contradiction=NEUTRAL,
            deontic_known=False,
            rule_exception=False,
            deontic_rationale="missing or unrecognised deontic: neutral",
        )

    overlap, contradiction, rationale = DEONTIC_TABLE[(label_a, label_b)]
    return DeonticScore(
        deontic_a=label_a,
        deontic_b=label_b,
        deontic_a_raw=raw_a,
        deontic_b_raw=raw_b,
        deontic_overlap=overlap,
        deontic_contradiction=contradiction,
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
        "table": [
            {"a": a, "b": b, "overlap": ov, "contradiction": co, "rationale": r}
            for (a, b), (ov, co, r) in _TABLE_HALF.items()
        ],
        "stats": stats,
        "scores": scores,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)

    print(f"Scored {stats['n_pairs']} pairs -> {out_path}")
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