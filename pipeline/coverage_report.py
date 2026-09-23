# -*- coding: utf-8 -*-
"""
Coverage report (2026-09-24) -- makes explicit what an external review's blueprint
insisted on and this pipeline didn't previously surface anywhere: "no budget cap may
silently convert unprocessed items to 'no conflict'" and "the complement of screened is
not_screened, never compatible". Before this, `findings_c1.json` alone could not
distinguish a candidate pair that was checked and cleared (NOT_A_CONFLICT) from a pair
that was never even generated as a candidate -- both are simply absent from that file.
This reads the adjudication cache (every verdict ever returned, not just the ones that
became findings) plus a fresh candidate count, and states the distinction explicitly.

Usage:
    python coverage_report.py [--model gpt-5.6-luna] [--no-semantic] [--semantic-k 8] [--semantic-k-within 3]
"""
import argparse
import json
from pathlib import Path

import networkx as nx

import detect_c1_contradiction as c1

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=c1.DEFAULT_MODEL)
    ap.add_argument("--semantic-k", type=int, default=8)
    ap.add_argument("--semantic-k-within", type=int, default=3)
    ap.add_argument("--no-semantic", action="store_true")
    args = ap.parse_args()

    from openai import OpenAI
    client = OpenAI()

    records = c1.load_all_norm_records()
    g = nx.read_gexf(DATA / "graph.gexf")

    semantic_pairs = None
    if not args.no_semantic:
        import similarity
        semantic_pairs = similarity.semantic_candidate_pairs(
            client, [r.text for r in records], k=args.semantic_k,
            groups=[r.instrument_id for r in records], k_within=args.semantic_k_within)

    candidates = c1.generate_candidates(records, g, semantic_pairs)
    exceptions = c1.load_conditional_exceptions()
    prepared = [p for p in (c1._prepare_candidate(cand, exceptions) for cand in candidates)
                if p["subtype"] is not None]
    AI_JUDGED = ("duty_conflict", "deontic_polarity_conflict")
    needs_llm = [p for p in prepared if p["subtype"] in AI_JUDGED]
    no_llm = [p for p in prepared if p["subtype"] not in AI_JUDGED]

    cache_path = DATA / "c1_adjudication_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    checked, unchecked = [], []
    for p in needs_llm:
        key = f"{args.model}::{c1._pair_id(p['a'], p['b'])}"
        (checked if key in cache else unchecked).append((p, cache.get(key)))

    from collections import Counter
    verdict_counts = Counter()
    for p, entry in checked:
        if entry["needs_recheck_reason"]:
            verdict_counts["needs_recheck"] += 1
        elif entry.get("challenge") and not entry["challenge"]["survives"]:
            verdict_counts["challenge_failed"] += 1
        else:
            verdict_counts[entry["verdict"]["verdict"]] += 1

    n_by_signal = Counter()
    for cand in candidates:
        for sig in ("graph_hit", "trigger_hit", "semantic_hit", "concept_hit"):
            if cand.get(sig):
                n_by_signal[sig] += 1

    unresolved_path = DATA / "stage6_unresolved_extractions.json"
    n_unresolved_extraction = len(json.loads(unresolved_path.read_text(encoding="utf-8"))) \
        if unresolved_path.exists() else 0

    print("=" * 70)
    print("C1 COVERAGE REPORT")
    print("=" * 70)
    print(f"\nStage 6 extraction:")
    print(f"  {len(records)} norms eligible for C1 (deontic in "
          f"{c1.ELIGIBLE_DEONTICS}, addressee_type known)")
    print(f"  {n_unresolved_extraction} paragraph(s) in the corpus still have NO extracted "
          f"norm at all (two extraction passes disagreed even after escalation, or both "
          f"failed validation) -- see data/stage6_unresolved_extractions.json. These "
          f"paragraphs' real content is NOT represented anywhere in C1 at all -- not as a "
          f"norm, not as a candidate, not as a finding.")

    print(f"\nCandidate generation:")
    print(f"  {len(candidates)} candidate pair(s) generated, by signal "
          f"(a pair can fire more than one):")
    for sig, n in n_by_signal.items():
        print(f"    {sig}: {n}")
    print(f"  {len(no_llm)} resolved deterministically (no AI needed)")
    print(f"  {len(needs_llm)} require AI adjudication")

    print(f"\nAI adjudication status (model: {args.model}):")
    print(f"  {len(checked)} of {len(needs_llm)} AI-judged pairs actually checked so far")
    print(f"  {len(unchecked)} of {len(needs_llm)} NEVER adjudicated at all")
    for verdict, n in verdict_counts.most_common():
        print(f"    {verdict}: {n}")

    print(f"\n*** {len(unchecked)} candidate pair(s) have never been sent to the AI. "
          f"Their absence from findings_c1.json means NOTHING -- it is NOT evidence "
          f"they're free of contradiction, only that they haven't been looked at yet. ***")
    print(f"*** Every eligible norm not captured in a candidate pair at all (graph/keyword/"
          f"semantic/concept signals all missed it) was never even considered against "
          f"anything -- that is a strictly larger, unmeasured gap on top of the above. ***")


if __name__ == "__main__":
    main()
