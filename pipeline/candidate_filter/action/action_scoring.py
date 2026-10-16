"""
Stage B: action candidate scoring (polarity-blind).

Scores how likely two norms are about the same action, as one candidate score.
The score does NOT distinguish duplication from contradiction: pairs above a
threshold go to LLM review, which decides the relation from the full text.

Scores (per pair, best match over all verb pairs):
    same axis, regardless of sign       1.0   (bewaren / bewaren, bewaren / verwijderen)
    identical non-stative lemma          1.0   (fallback for lemmas not in the lexicon)
    related axes, regardless of sign     0.5   (verstrekken / melden)
    both mapped, nothing in common      -0.5   (verstrekken / bewaren)
    missing on either side               0.0   (empty lemma, no lexicon match, unknown id)

Negation and deontic are ignored on purpose. Each pair also gets a `generic`
flag when its best match is only on generic axes (ensure, assess, perform, ...),
so the downstream ranking can discount it together with object/actor overlap.

Inputs:
  - stageA_pairs.json  ({"norm_ids": [...], "pairs": [[i, j], ...]}),
    or --all-pairs to score every pair of norms in the input
  - AI_ACT_norms.json (re-extracts verbs) OR action_verbs.py JSON output (--verbs)

Usage:
  python action_scoring.py --demo
  python action_scoring.py --pairs stageA_pairs.json --norms AI_ACT_norms.json \\
      --out stageB_action_scores.json
  python action_scoring.py --pairs stageA_pairs.json --verbs action_verbs.json \\
      --out stageB_action_scores.json
  python action_scoring.py --all-pairs --norms AI_ACT_norms.json --min-score 0.5 \\
      --out stageB_candidates.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from action_verbs import (
    extract_action_verbs,
    inner_norm,
    load_norm_records,
    norm_action_context,
)
from verb_lexicon import GENERIC_AXES, RELATED_AXES, STATIVE_LEMMAS


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

SAME_AXIS_SCORE = 1.0
SAME_LEMMA_SCORE = 1.0
RELATED_SCORE = 0.5
UNRELATED_SCORE = -0.5
MISSING_SCORE = 0.0


# ---------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class AxisHit:
    axis: str
    family: str
    matched: str


@dataclass
class ActionProfile:
    status: str                      # "verb" | "none" | "empty"
    lemma: str | None
    hits: list[AxisHit] = field(default_factory=list)

    @property
    def axes(self) -> set[str]:
        return {hit.axis for hit in self.hits}


@dataclass
class ActionScore:
    action_status_a: str
    action_status_b: str
    action_score: float
    action_relation: str             # same_axis | same_lemma | related | unrelated | missing
    generic: bool
    basis: str | None


# ---------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------

def _norm_id(record: dict) -> str | None:
    value = record.get("norm_id", record.get("id"))
    return str(value) if value is not None else None


def _normalise_lemma(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    text = " ".join(text.lower().split())
    return text or None


def build_profile(extraction: dict, lemma: Any = None) -> ActionProfile:
    """Turn an action_verbs.py extraction into the set of axes it touches."""
    lemma_norm = _normalise_lemma(lemma)
    verbs = [v for v in extraction.get("verbs") or [] if isinstance(v, dict)]

    hits: list[AxisHit] = []
    for verb in verbs:
        family = str(verb.get("family") or "unknown")
        matched = str(verb.get("matched") or "")
        for item in verb.get("axes") or []:
            axis = item.get("axis") if isinstance(item, dict) else item
            if axis:
                hits.append(AxisHit(str(axis), family, matched))

    if hits:
        status = "verb"
    elif lemma_norm is None:
        status = "empty"
    else:
        status = "none"

    return ActionProfile(status=status, lemma=lemma_norm, hits=hits)


def profile_from_text(text: Any) -> ActionProfile:
    return build_profile(extract_action_verbs(text), lemma=text)


# ---------------------------------------------------------------------
# Pair scoring
# ---------------------------------------------------------------------

def _describe(a: AxisHit, b: AxisHit, note: str = "") -> str:
    out = f"{a.family}:{a.axis} '{a.matched}' vs {b.family}:{b.axis} '{b.matched}'"
    return f"{out} [{note}]" if note else out


def score_action_pair(a: ActionProfile, b: ActionProfile) -> ActionScore:
    """Polarity-blind candidate score for one pair of actions."""

    # Identical lemma that the lexicon doesn't know: still the same action.
    # (Mapped identical lemmas fall through to the same-axis branch below.)
    if (
        a.lemma is not None
        and a.lemma == b.lemma
        and a.lemma not in STATIVE_LEMMAS
        and not (a.axes and b.axes)
    ):
        generic = bool(a.axes) and a.axes <= GENERIC_AXES
        return ActionScore(
            a.status, b.status, SAME_LEMMA_SCORE, "same_lemma", generic,
            f"identical lemma '{a.lemma}'",
        )

    if a.status != "verb" or b.status != "verb":
        return ActionScore(
            a.status, b.status, MISSING_SCORE, "missing", False, None,
        )

    # Same axis (sign ignored). Prefer a specific axis over a generic one.
    shared = a.axes & b.axes
    if shared:
        specific = shared - GENERIC_AXES
        axis = sorted(specific or shared)[0]
        ha = next(h for h in a.hits if h.axis == axis)
        hb = next(h for h in b.hits if h.axis == axis)
        return ActionScore(
            a.status, b.status, SAME_AXIS_SCORE, "same_axis",
            not specific, _describe(ha, hb),
        )

    # Related axes (sign ignored).
    related = [
        (ha, hb) for ha in a.hits for hb in b.hits
        if frozenset((ha.axis, hb.axis)) in RELATED_AXES
    ]
    if related:
        related.sort(
            key=lambda p: (p[0].axis in GENERIC_AXES) + (p[1].axis in GENERIC_AXES)
        )
        ha, hb = related[0]
        generic = ha.axis in GENERIC_AXES and hb.axis in GENERIC_AXES
        return ActionScore(
            a.status, b.status, RELATED_SCORE, "related", generic,
            _describe(ha, hb, "related axes"),
        )

    ha, hb = a.hits[0], b.hits[0]
    return ActionScore(
        a.status, b.status, UNRELATED_SCORE, "unrelated", False,
        _describe(ha, hb, "no shared or related axis"),
    )


# ---------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------

def build_profiles_from_norms(
    norms_path: Path, span_fallback: bool = False,
) -> dict[str, ActionProfile]:
    profiles: dict[str, ActionProfile] = {}

    for record in load_norm_records(norms_path):
        norm = inner_norm(record)
        norm_id = _norm_id(record) or _norm_id(norm)
        if norm_id is None:
            continue

        text = norm_action_context(record, norm, span_fallback)
        profiles[norm_id] = build_profile(extract_action_verbs(text), lemma=text)

    return profiles


def build_profiles_from_verbs(verbs_path: Path) -> dict[str, ActionProfile]:
    """Read the JSON export of action_verbs.py."""
    with verbs_path.open(encoding="utf-8") as file:
        data = json.load(file)

    records = data.get("norms") if isinstance(data, dict) else data
    if not isinstance(records, list):
        raise ValueError(
            "Verb JSON must be a list or a dictionary with a 'norms' list."
        )

    profiles: dict[str, ActionProfile] = {}
    for record in records:
        norm_id = _norm_id(record)
        if norm_id is None:
            continue
        lemma = record.get("action_clean") or record.get("action")
        profiles[norm_id] = build_profile(record, lemma=lemma)

    return profiles


def stage_a_pair_id(i: int, j: int, n: int) -> int:
    """Same formula as stageA_filtering.pair_id: the position of (i, j), i < j, among
    all n*(n-1)/2 pairs of the Stage A norm list."""
    return i * n - i * (i + 1) // 2 + (j - i - 1)


def load_pairs(path: Path) -> tuple[list[str], list[tuple[int, int, int]], dict]:
    """Read stageA_pairs.json. Pairs are [pair_id, i, j]; older files with plain [i, j]
    pairs get the id Stage A would have given them."""
    with path.open(encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError("Pairs JSON must be an object.")

    norm_ids = data.get("norm_ids")
    raw_pairs = data.get("pairs")
    if not isinstance(norm_ids, list) or not isinstance(raw_pairs, list):
        raise ValueError("Pairs JSON must contain 'norm_ids' and 'pairs' lists.")

    n = len(norm_ids)
    pairs = []
    seen: set[int] = set()
    for pair in raw_pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) not in (2, 3):
            raise ValueError(f"Invalid pair entry: {pair!r}")
        if len(pair) == 3:
            pid, i, j = int(pair[0]), int(pair[1]), int(pair[2])
        else:
            i, j = int(pair[0]), int(pair[1])
            pid = stage_a_pair_id(i, j, n) if i < j else None
        if not (0 <= i < n and 0 <= j < n):
            raise ValueError(f"Pair index out of range: {(i, j)}")
        if pid is None or pid in seen:
            raise ValueError(f"Missing or duplicate pair id in entry: {pair!r}")
        seen.add(pid)
        pairs.append((pid, i, j))

    metadata = {
        k: v for k, v in data.items() if k not in {"norm_ids", "pairs", "pair_format"}
    }
    return [str(v) for v in norm_ids], pairs, metadata


# ---------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------

def _write_output(output: dict, file) -> None:
    """Write the output as indented JSON, but with one line per score row.
    Plain indent=2 would spread each row over several lines."""
    rows = output.pop("scores")
    head = json.dumps(output, ensure_ascii=False, indent=2)
    output["scores"] = rows

    file.write(head[:-2])                        # drop the closing "\n}"
    file.write(',\n  "scores": [')
    for n, row in enumerate(rows):
        file.write("," if n else "")
        file.write("\n    " + json.dumps(row, ensure_ascii=False))
    file.write("\n  ]\n}\n" if rows else "]\n}\n")


# ---------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------

def run(
    pairs_path: Path | None,
    norms_path: Path | None,
    verbs_path: Path | None,
    out_path: Path,
    all_pairs: bool = False,
    min_score: float | None = None,
    span_fallback: bool = False,
) -> None:
    if verbs_path is not None:
        profiles = build_profiles_from_verbs(verbs_path)
        source = str(verbs_path)
    elif norms_path is not None:
        profiles = build_profiles_from_norms(norms_path, span_fallback)
        source = str(norms_path)
    else:
        raise ValueError("Provide either --norms or --verbs.")

    if all_pairs:
        norm_ids = list(profiles)
        pairs = [
            (None, i, j) for i, j in itertools.combinations(range(len(norm_ids)), 2)
        ]
        stage_a_meta: dict = {"all_pairs": True}
    else:
        if pairs_path is None:
            raise ValueError("Provide --pairs or --all-pairs.")
        norm_ids, pairs, stage_a_meta = load_pairs(pairs_path)

    missing_profile = ActionProfile("empty", None)
    missing_ids: set[str] = set()
    relation_counts: Counter[str] = Counter()
    # One row per pair: [pair_id, action_score], with pair_id taken from
    # stageA_pairs.json. --all-pairs has no Stage A ids, so it writes
    # [norm_id_a, norm_id_b, action_score] instead.
    scores: list[list] = []
    pair_id_source = "norm_ids" if all_pairs else "stageA_pair_id"

    for pair_id, i, j in pairs:
        id_a, id_b = norm_ids[i], norm_ids[j]
        pa, pb = profiles.get(id_a), profiles.get(id_b)
        if pa is None:
            missing_ids.add(id_a)
            pa = missing_profile
        if pb is None:
            missing_ids.add(id_b)
            pb = missing_profile

        result = score_action_pair(pa, pb)
        relation_counts[result.action_relation] += 1

        if min_score is not None and result.action_score < min_score:
            continue

        if all_pairs:
            scores.append([id_a, id_b, result.action_score])
        else:
            scores.append([pair_id, result.action_score])

    norm_status = Counter(p.status for p in profiles.values())
    stats = {
        "n_pairs": len(pairs),
        "n_pairs_written": len(scores),
        "n_norms": len(profiles),
        "norm_status": dict(norm_status.most_common()),
        "pair_relation": dict(relation_counts.most_common()),
        "norm_ids_missing": sorted(missing_ids),
    }

    output = {
        "stage": "B-action-candidates",
        "input_pairs": str(pairs_path) if pairs_path else None,
        "input_verbs_or_norms": source,
        "stage_a_metadata": stage_a_meta,
        "parameters": {
            "same_axis_score": SAME_AXIS_SCORE,
            "same_lemma_score": SAME_LEMMA_SCORE,
            "related_score": RELATED_SCORE,
            "unrelated_score": UNRELATED_SCORE,
            "missing_score": MISSING_SCORE,
            "polarity": "ignored",
            "negation": "ignored",
            "min_score": min_score,
        },
        "stats": stats,
        "score_format": (
            ["norm_id_a", "norm_id_b", "action_score"] if all_pairs
            else ["pair_id", "action_score"]
        ),
        "pair_id_source": pair_id_source,
        "scores": scores,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as file:
        _write_output(output, file)

    print(f"Scored {len(pairs)} pairs -> {out_path} ({len(scores)} written)")
    print("Norm status:", stats["norm_status"])
    print("Pair relation:", stats["pair_relation"])
    if missing_ids:
        print(
            f"Warning: {len(missing_ids)} norm IDs were not found (scored 0).",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------

DEMO_PAIRS = [
    ("verstrekken", "verstrekken"),
    ("bewaren", "verwijderen"),               # antonyms: same axis, sign ignored
    ("in de handel brengen", "uit de handel nemen"),
    ("controleren", "toetsen"),               # same family, generic
    ("verstrekken", "melden"),                # related axes
    ("verstrekken", "bewaren"),               # unrelated
    ("vervuld worden", "vervuld worden"),     # unmapped but identical lemma
    ("zijn", "zijn"),                         # stative: no lemma fallback
    ("", "verstrekken"),                      # missing
    ("onbekendwerkwoord", "verstrekken"),     # no lexicon match
]


def demo() -> None:
    for a, b in DEMO_PAIRS:
        r = score_action_pair(profile_from_text(a), profile_from_text(b))
        flag = " generic" if r.generic else ""
        print(f"{a!r:26} {b!r:24} {r.action_score:+.1f}  {r.action_relation}{flag}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Polarity-blind action candidate scoring for norm pairs."
    )
    parser.add_argument("--pairs", type=Path, default=Path("data/conflict/stageA_pairs.json"))
    parser.add_argument("--norms", type=Path, default=Path("data/new_norms/original_norms.json"))
    parser.add_argument(
        "--verbs", type=Path, default=None,
        help="Use action_verbs.py JSON output instead of re-extracting verbs.",
    )
    parser.add_argument("--out", type=Path, default=Path("data/conflict/action/action_scores.json"))
    parser.add_argument(
        "--all-pairs", action="store_true",
        help="Score every pair of norms in the input (no Stage A pairs file).",
    )
    parser.add_argument(
        "--min-score", type=float, default=None,
        help="Only write pairs with action_score >= this value (default: all).",
    )
    parser.add_argument(
        "--span-fallback", action="store_true",
        help="Use action_span/action when action_lemma is empty (--norms only).",
    )
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()

    if args.demo:
        demo()
        return

    run(
        pairs_path=args.pairs,
        norms_path=None if args.verbs else args.norms,
        verbs_path=args.verbs,
        out_path=args.out,
        all_pairs=args.all_pairs,
        min_score=args.min_score,
        span_fallback=args.span_fallback,
    )


if __name__ == "__main__":
    main()