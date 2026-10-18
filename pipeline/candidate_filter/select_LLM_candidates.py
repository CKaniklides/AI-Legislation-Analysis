#!/usr/bin/env python3
"""select_candidates.py - select the pairs that go to LLM review.

Reads combined_pair_scores.jsonl (from combine_scores.py, sorted by `combined`
descending, ties by pair_id), selects a top tier under a fixed review budget,
adds a small calibration sample from the pairs that were NOT selected, links
each pair to the two norm cards (norm_cards.py), and writes the review queue.

Norm cards
  Cards are read from LLM_norm_cards.jsonl (one card per norm, built by
  norm_cards.py). The queue stores only each card's card_hash, not the card itself:
  a norm can appear in up to PER_NORM_CAP pairs, so copying cards into the queue
  would repeat the same text that many times. The LLM step loads the card file once
  and builds each prompt from the two cards (llm_review.pair_text()).
  Build the cards for ALL norms (norm_cards.py without --queue), since the queue
  does not exist yet when the cards are needed here.

By default the script only writes the queue. With --send it then passes the
queue to llm_review.py (one LLM pass per pair, code checks, one retry on failure).

=============================================================================
SELECTION
=============================================================================

Variables (module constants, all overridable on the command line):

  BUDGET              max pairs in the top tier (the LLM review budget)
  MIN_SCORE           no pair below this `combined` score enters the top tier,
                      even if the budget is not used up
  PER_NORM_CAP        max top-tier pairs any single norm may take part in, so a
                      few norms with common verbs and broad objects cannot take
                      a large share of the budget
  MAX_TIE_OVERSHOOT   fraction of BUDGET the top tier may exceed the budget by
                      to finish the tie group at the cut-off (0 = hard budget)
  CALIBRATION_SIZE    fraction of BUDGET added as a calibration sample from the
                      pairs that were not selected
  CALIBRATION_EDGES   score edges that split the not-selected pairs into bands;
                      the calibration sample is spread evenly over the bands

1. Top tier. Rows are read in score order. A row is taken if
     combined >= MIN_SCORE  and  neither norm has reached PER_NORM_CAP.
   Selection stops when BUDGET pairs are taken.

2. Tie group at the cut-off. Many pairs share a score, so a hard budget cut
   would split a tie group at an arbitrary point. After the budget is reached,
   rows with exactly the cut-off score are still taken (same cap rule), up to
   BUDGET x MAX_TIE_OVERSHOOT extra pairs. If the tie group is larger than that,
   it is cut in pair_id order, which combine_scores.py already uses and which is
   unrelated to the source instrument.

3. Calibration sample. Every pair not in the top tier (below the cut-off,
   capped, or beyond the budget) falls into a band:
     band 0: combined >= MIN_SCORE         (good enough, but budget or cap excluded it)
     band k: CALIBRATION_EDGES[k] <= combined < previous edge
     last:   below the lowest edge
   Each band gets an equal share of the sample (unused shares of small bands
   go to the others). Within a band, the pairs with the lowest
   sha256(pair_id + CALIBRATION_SEED) are taken: a uniform random sample that is
   reproducible and does not depend on file order. The LLM verdicts on these
   pairs estimate how many real contradictions/duplications each band holds,
   which is the data needed to calibrate the weights and the cut-off.

   Calibration pairs are marked selection = "calibration" so their verdicts are
   never mixed into the top-tier results by accident.

=============================================================================
OUTPUT
=============================================================================

  review_queue.jsonl         one row per pair to review:
      pair_id, selection (top | tie_extension | calibration), calibration_band,
      rank (position in the score file, 1-based), all score fields from
      combine_scores.py, and card_hash_a / card_hash_b.
      Pairs with a norm that has no card are skipped during selection (counted as
      skipped_no_card), so they never take a budget or calibration slot.
      With --render, also pair_text: the rendered prompt block, for inspection.
  review_queue_config.json   variables used, cut-off score, and counts

Usage
-----
  python select_candidates.py
  python select_candidates.py --budget 3000 --min-score 0.55 --per-norm-cap 15
  python select_candidates.py --calibration-size 0 --max-tie-overshoot 0
  python select_candidates.py --render      # also write the rendered pair text
  python select_candidates.py --send --model gpt-5.6-luna   # select, then review
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Iterator

# --- Selection variables --------------------------------------------------------
BUDGET = 2000
MIN_SCORE = 0.5
PER_NORM_CAP = 20
MAX_TIE_OVERSHOOT = 0.10
CALIBRATION_SIZE = 0.03
CALIBRATION_EDGES = (0.4, 0.25)    # below MIN_SCORE; edges >= MIN_SCORE are ignored
CALIBRATION_SEED = "calibration-v1"

DEFAULT_SCORES = Path("data/conflict/combined_pair_scores.jsonl")
DEFAULT_CARDS = Path("data/new_norms/LLM_norm_cards.jsonl")
DEFAULT_OUT = Path("data/conflict/review_queue.jsonl")


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def read_jsonl(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def load_cards(path: Path) -> dict[str, dict]:
    """Norm cards (norm_cards.py output) indexed by norm_id."""
    cards: dict[str, dict] = {}
    for c in read_jsonl(path):
        cards[str(c["norm_id"])] = c
    return cards


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def band_edges(min_score: float, edges: tuple[float, ...]) -> list[float]:
    """Descending lower edges: [min_score, e1, e2, ...] with every e < min_score."""
    return [min_score] + sorted({e for e in edges if e < min_score}, reverse=True)


def band_of(score: float, edges: list[float]) -> int:
    for k, edge in enumerate(edges):
        if score >= edge:
            return k
    return len(edges)


def band_label(k: int, edges: list[float]) -> str:
    if k == 0:
        return f">= {edges[0]} (not selected)"
    if k == len(edges):
        return f"< {edges[-1]}"
    return f"{edges[k]} - {edges[k - 1]}"


def sample_key(pid: str, seed: str) -> int:
    return int(hashlib.sha256(f"{pid}:{seed}".encode("utf-8")).hexdigest()[:16], 16)


def select(rows: Iterator[dict], budget: int, min_score: float, per_norm_cap: int,
           max_tie_overshoot: float, calibration_n: int, edges: list[float],
           seed: str, has_card: set[str] | None = None) -> tuple[list[dict], list[dict], dict]:
    """One streaming pass. Returns (top tier, calibration sample, stats).

    The calibration pool is held as one bounded max-heap per band (size
    calibration_n each), so memory stays small however large the score file is."""
    n_bands = len(edges) + 1
    tie_limit = int(budget * max_tie_overshoot)
    norm_count: Counter[str] = Counter()
    top: list[dict] = []
    heaps: list[list[tuple[int, int, dict]]] = [[] for _ in range(n_bands)]
    band_sizes = Counter()
    stats = Counter()
    missing_cards: set[str] = set()

    cutoff: float | None = None      # score of the last pair taken within the budget
    tie_taken = 0
    prev = float("inf")
    rank = 0

    for rank, row in enumerate(rows, 1):
        score = float(row["combined"])
        if score > prev + 1e-9:
            sys.exit(f"Score file is not sorted by `combined` descending (row {rank}). "
                     f"Use the output of combine_scores.py unchanged.")
        prev = score
        row["rank"] = rank
        a, b = str(row["norm_a"]), str(row["norm_b"])
        if has_card is not None and (a not in has_card or b not in has_card):
            stats["skipped_no_card"] += 1
            for n in (a, b):
                if n not in has_card:
                    missing_cards.add(n)
            continue

        taken = None
        if score >= min_score:
            capped = norm_count[a] >= per_norm_cap or norm_count[b] >= per_norm_cap
            if len(top) < budget:
                if capped:
                    stats["skipped_per_norm_cap"] += 1
                else:
                    taken = "top"
            elif cutoff is not None and abs(score - cutoff) < 1e-9:
                if capped:
                    stats["skipped_per_norm_cap"] += 1
                elif tie_taken < tie_limit:
                    taken = "tie_extension"
                    tie_taken += 1
                else:
                    stats["tie_group_cut"] += 1
            else:
                stats["beyond_budget"] += 1
        else:
            stats["below_min_score"] += 1

        if taken:
            row["selection"] = taken
            row["calibration_band"] = None
            top.append(row)
            norm_count[a] += 1
            norm_count[b] += 1
            if taken == "top" and len(top) == budget:
                cutoff = score
            continue

        # Not selected: candidate for the calibration sample.
        k = band_of(score, edges)
        band_sizes[k] += 1
        if calibration_n > 0:
            key = sample_key(row["pair_id"], seed)
            item = (-key, rank, row)               # max-heap on key via negation
            if len(heaps[k]) < calibration_n:
                heapq.heappush(heaps[k], item)
            elif key < -heaps[k][0][0]:
                heapq.heapreplace(heaps[k], item)

    # Equal share per band; leftover from small bands goes to the others.
    calibration: list[dict] = []
    pools = [sorted(((-nk, r) for nk, _, r in h), key=lambda t: t[0]) for h in heaps]
    remaining = calibration_n
    open_bands = [k for k in range(n_bands) if pools[k]]
    taken_per_band = Counter()
    while remaining > 0 and open_bands:
        share = max(1, remaining // len(open_bands))
        next_open = []
        for k in open_bands:
            if remaining == 0:
                break
            start = taken_per_band[k]
            chunk = pools[k][start:start + min(share, remaining)]
            for _, r in chunk:
                r["selection"] = "calibration"
                r["calibration_band"] = band_label(k, edges)
                calibration.append(r)
            taken_per_band[k] += len(chunk)
            remaining -= len(chunk)
            if taken_per_band[k] < len(pools[k]):
                next_open.append(k)
        open_bands = next_open

    stats = dict(stats)              # plain dict: Counter.update would add, not set
    stats.update({
        "rows_read": rank,
        "top_tier": sum(1 for r in top if r["selection"] == "top"),
        "tie_extension": tie_taken,
        "calibration": len(calibration),
        "cutoff_score": cutoff if cutoff is not None else (top[-1]["combined"] if top else None),
        "budget_filled": cutoff is not None,
        "norms_in_top_tier": len(norm_count),
        "norms_without_card": sorted(missing_cards),
        "norms_at_cap": sum(1 for v in norm_count.values() if v >= per_norm_cap),
        "not_selected_per_band": {band_label(k, edges): band_sizes[k] for k in range(n_bands)},
        "calibration_per_band": {band_label(k, edges): taken_per_band[k] for k in range(n_bands)},
    })
    return top, calibration, stats


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------

def attach_cards(rows: list[dict], cards: dict[str, dict]) -> None:
    """Add both card hashes to each row (every selected pair has both cards)."""
    for r in rows:
        r["card_hash_a"] = cards[str(r["norm_a"])]["card_hash"]
        r["card_hash_b"] = cards[str(r["norm_b"])]["card_hash"]


def pair_text(row: dict, cards: dict[str, dict], facts: str | None = None) -> str:
    """The prompt block for one queue row (see llm_review.pair_text)."""
    from llm_review import pair_text as _pair_text
    return _pair_text(row, cards, facts)


def submit_for_review(queue: list[dict], cards: dict[str, dict],
                      args: argparse.Namespace) -> None:
    """Send the queue to LLM review (llm_review.py). Top tier first, then the
    calibration sample; cached pairs are not sent again."""
    from llm_review import review_queue
    review_queue(queue, cards, model=args.model, effort=args.reasoning_effort,
                 concurrency=args.concurrency)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Select norm pairs for LLM review.")
    p.add_argument("--scores", type=Path, default=DEFAULT_SCORES,
                   help="combined_pair_scores.jsonl from combine_scores.py")
    p.add_argument("--cards", type=Path, default=DEFAULT_CARDS,
                   help="norm cards from norm_cards.py (LLM_norm_cards.jsonl)")
    p.add_argument("--render", action="store_true",
                   help="also store the rendered pair text in each queue row (for inspection)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--budget", type=int, default=BUDGET)
    p.add_argument("--min-score", type=float, default=MIN_SCORE)
    p.add_argument("--per-norm-cap", type=int, default=PER_NORM_CAP)
    p.add_argument("--max-tie-overshoot", type=float, default=MAX_TIE_OVERSHOOT,
                   help="fraction of the budget allowed to finish the cut-off tie group")
    p.add_argument("--calibration-size", type=float, default=CALIBRATION_SIZE,
                   help="calibration sample as a fraction of the budget (0 = none)")
    p.add_argument("--calibration-edges", type=float, nargs="*", default=list(CALIBRATION_EDGES),
                   help="band edges below --min-score for the calibration sample")
    p.add_argument("--calibration-seed", default=CALIBRATION_SEED,
                   help="change to draw a different (but still reproducible) sample")
    p.add_argument("--send", action="store_true",
                   help="send the queue to LLM review (llm_review.py) after writing it")
    p.add_argument("--model", default="gpt-5.6-luna", help="LLM model, with --send")
    p.add_argument("--reasoning-effort", default="medium",
                   choices=["minimal", "low", "medium", "high"], help="with --send")
    p.add_argument("--concurrency", type=int, default=8, help="parallel LLM calls, with --send")
    args = p.parse_args()

    if args.budget <= 0 or args.per_norm_cap <= 0:
        sys.exit("--budget and --per-norm-cap must be positive.")
    if not (0 <= args.max_tie_overshoot and 0 <= args.calibration_size):
        sys.exit("--max-tie-overshoot and --calibration-size must be >= 0.")

    edges = band_edges(args.min_score, tuple(args.calibration_edges))
    calibration_n = round(args.budget * args.calibration_size)

    cards = load_cards(args.cards)
    top, calibration, stats = select(
        read_jsonl(args.scores), args.budget, args.min_score, args.per_norm_cap,
        args.max_tie_overshoot, calibration_n, edges, args.calibration_seed,
        has_card=set(cards))
    missing = stats.pop("norms_without_card")

    queue = top + sorted(calibration, key=lambda r: r["rank"])
    attach_cards(queue, cards)
    if args.render:
        for r in queue:
            r["pair_text"] = pair_text(r, cards)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(args.out, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in queue))
    config = {
        "inputs": {"scores": str(args.scores), "cards": str(args.cards)},
        "variables": {
            "budget": args.budget, "min_score": args.min_score,
            "per_norm_cap": args.per_norm_cap, "max_tie_overshoot": args.max_tie_overshoot,
            "calibration_size": args.calibration_size, "calibration_n": calibration_n,
            "calibration_edges": edges, "calibration_seed": args.calibration_seed,
        },
        "stats": {**stats, "queue_size": len(queue), "norm_ids_without_card": len(missing)},
    }
    atomic_write_text(args.out.with_name("review_queue_config.json"),
                      json.dumps(config, indent=2, ensure_ascii=False))

    # --- Summary ---------------------------------------------------------------
    print(f"Read {stats['rows_read']} scored pairs from {args.scores}")
    print(f"Top tier: {stats['top_tier']} pairs (budget {args.budget}), "
          f"+{stats['tie_extension']} to finish the tie group at cut-off "
          f"{stats['cutoff_score']}")
    if not stats["budget_filled"]:
        print(f"  Budget not filled: only {stats['top_tier']} pairs reach "
              f"--min-score {args.min_score} within the per-norm cap.")
    print(f"Skipped: {stats.get('skipped_per_norm_cap', 0)} by per-norm cap, "
          f"{stats.get('tie_group_cut', 0)} from an oversized tie group, "
          f"{stats.get('beyond_budget', 0)} beyond budget, "
          f"{stats.get('below_min_score', 0)} below min score, "
          f"{stats.get('skipped_no_card', 0)} without a norm card")
    print(f"Norms in top tier: {stats['norms_in_top_tier']} "
          f"({stats['norms_at_cap']} at the cap of {args.per_norm_cap})")
    print(f"Calibration sample: {stats['calibration']} pairs "
          f"{stats['calibration_per_band']}")
    print(f"Review queue: {len(queue)} pairs -> {args.out}")
    if missing:
        print(f"Warning: {len(missing)} norm_ids have no card in {args.cards}; their "
              f"{stats.get('skipped_no_card', 0)} pairs were skipped. Rebuild the cards for "
              f"all norms (norm_cards.py without --queue). E.g. {missing[:3]}", file=sys.stderr)

    if args.send:
        submit_for_review(queue, cards, args)


if __name__ == "__main__":
    main()