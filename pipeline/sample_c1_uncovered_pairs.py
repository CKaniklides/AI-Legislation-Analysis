# -*- coding: utf-8 -*-
"""
One-off (2026-09-26): C1's adjudication cache turned out to cover only 6,404 of the
14,950 candidate pairs the CURRENT corpus actually produces -- the corpus grew across
three incremental runs (960 -> 1,474 -> 1,715 eligible norms) and the most recent run
was an explicit --priority-only pass over one signal, never a full sweep. So "C1 found
zero contradictions" was really "C1 found zero contradictions in 43% of its own
candidate pairs" -- a materially incomplete answer, not a real negative result.

This script does two things, per the user's explicit choice ("sample first" -- check
yield before committing to the full 8,546-pair remainder):

1. Re-adjudicates the ~392 pairs already stuck at INSUFFICIENT_EVIDENCE/needs_recheck,
   now WITH the reference-context fix (paragraph_text_index/reverse_reference_index,
   see detect_c1_contradiction.py's own note on this) -- these were confirmed (by
   direct sampling) to often be missing the referenced paragraph's substance, not
   genuinely inconclusive.
2. Draws a stratified sample of ~1,000 of the 8,546 never-adjudicated pairs -- ALL 22
   deontic_polarity_conflict pairs (rare, structurally the most likely genuine
   contradiction shape) plus a proportional sample of duty_conflict pairs across each
   discovery-signal combination (semantic/trigger/graph/concept hits), so the sample
   doesn't just reflect whichever signal happens to be most common.

Both sets go through the same adjudicate_duty_conflict() as a real run would, results
are written into the SAME c1_adjudication_cache.json under the same key format
main() uses, so a later full run (or rebuild_findings_c1_from_cache.py) picks them up
for free. A fixed random seed makes the sample reproducible.
"""
import argparse
import json
import random
import sys
import threading
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_c1_contradiction as c1
import networkx as nx

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SAMPLE_SEED = 20260926
TARGET_SAMPLE_SIZE = 1000
CONCURRENCY = 10


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                     help="build the repair + sample sets and print counts, no LLM calls, "
                          "nothing written to the cache")
    args = ap.parse_args()

    from openai import OpenAI
    client = OpenAI()
    model = c1.DEFAULT_MODEL

    print("Regenerating the current full candidate set...", flush=True)
    records = c1.load_all_norm_records()
    g = nx.read_gexf(DATA / "graph.gexf")
    import similarity
    semantic_pairs = similarity.semantic_candidate_pairs(
        client, [r.text for r in records], k=8,
        groups=[r.instrument_id for r in records], k_within=3)
    exceptions = c1.load_conditional_exceptions()
    candidates = c1.generate_candidates(records, g, semantic_pairs)
    prepared = [p for p in (c1._prepare_candidate(cd, exceptions) for cd in candidates)
                if p["subtype"] is not None]
    AI_JUDGED = ("duty_conflict", "deontic_polarity_conflict")
    needs_llm = [p for p in prepared if p["subtype"] in AI_JUDGED]
    print(f"  {len(needs_llm)} AI-judged candidates total (current corpus)", flush=True)

    cache_path = DATA / "c1_adjudication_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    cache_lock = threading.Lock()

    def cache_key(p):
        return f"{model}::{c1._pair_id(p['a'], p['b'])}"

    def _save_cache():
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    cached_p = [p for p in needs_llm if cache_key(p) in cache]
    uncached_p = [p for p in needs_llm if cache_key(p) not in cache]
    print(f"  {len(cached_p)} already adjudicated, {len(uncached_p)} never adjudicated", flush=True)

    # --- Population 1: stuck pairs (INSUFFICIENT_EVIDENCE / needs_recheck) -- purge
    # from the cache so they're genuinely re-run with reference context this time.
    stuck_p = []
    for p in cached_p:
        entry = cache[cache_key(p)]
        if entry.get("needs_recheck_reason") or entry["verdict"]["verdict"] == "INSUFFICIENT_EVIDENCE":
            stuck_p.append(p)
    print(f"  {len(stuck_p)} stuck pairs (INSUFFICIENT_EVIDENCE/needs_recheck) -- "
          f"will be re-adjudicated with reference context", flush=True)
    for p in stuck_p:
        del cache[cache_key(p)]

    # --- Population 2: stratified sample of the never-adjudicated pool ---
    polarity_p = [p for p in uncached_p if p["subtype"] == "deontic_polarity_conflict"]
    duty_p = [p for p in uncached_p if p["subtype"] == "duty_conflict"]
    print(f"  never-adjudicated: {len(polarity_p)} deontic_polarity_conflict "
          f"(all included), {len(duty_p)} duty_conflict (stratified sample)", flush=True)

    def signal_combo(p):
        c_ = p["c"]
        return tuple(sorted(k for k in ("graph_hit", "trigger_hit", "semantic_hit", "concept_hit")
                             if c_[k]))

    by_signal = defaultdict(list)
    for p in duty_p:
        by_signal[signal_combo(p)].append(p)

    rng = random.Random(SAMPLE_SEED)
    remaining_budget = TARGET_SAMPLE_SIZE - len(polarity_p)
    duty_sample = []
    for combo, bucket in sorted(by_signal.items(), key=lambda kv: -len(kv[1])):
        quota = round(remaining_budget * len(bucket) / len(duty_p))
        quota = min(max(quota, 1), len(bucket))  # floor of 1 so no signal bucket is silently skipped
        chosen = rng.sample(bucket, quota) if quota < len(bucket) else list(bucket)
        duty_sample.extend(chosen)
        print(f"    signal {combo or ('none',)}: {len(bucket)} available -> {len(chosen)} sampled",
              flush=True)

    sampled_p = polarity_p + duty_sample
    print(f"  total sampled from never-adjudicated pool: {len(sampled_p)}", flush=True)

    # Audit trail: record exactly which pairs were sampled/purged, for reproducibility.
    audit_path = DATA / "c1_sample_2026-09-26_audit.json"
    audit_path.write_text(json.dumps({
        "seed": SAMPLE_SEED,
        "stuck_pairs_repaired": [c1._pair_id(p["a"], p["b"]) for p in stuck_p],
        "sampled_never_adjudicated": [c1._pair_id(p["a"], p["b"]) for p in sampled_p],
    }, indent=1), encoding="utf-8")

    to_run = stuck_p + sampled_p
    print(f"\nTotal pairs to adjudicate this run: {len(to_run)} "
          f"({len(stuck_p)} repaired + {len(sampled_p)} newly sampled)", flush=True)

    if args.dry_run:
        print("[dry-run] stopping before any LLM calls -- cache purge above was in-memory "
              "only and was NOT saved to disk.", flush=True)
        return

    paragraph_text_index = c1._build_paragraph_text_index(records)
    reverse_reference_index = c1._build_reverse_reference_index(records)

    stuck_ids = {id(p) for p in stuck_p}
    results = {}  # id(p) -> AdjudicationResult, keyed by pair identity for tallying
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        future_to_p = {
            ex.submit(c1.adjudicate_duty_conflict, client, model, p["a"], p["b"],
                      paragraph_text_index, reverse_reference_index): p
            for p in to_run
        }
        for done, fut in enumerate(as_completed(future_to_p), 1):
            p = future_to_p[fut]
            a, b = p["a"], p["b"]
            label = f"{a.instrument_id} art.{a.article} <-> {b.instrument_id} art.{b.article}"
            try:
                result = fut.result()
            except Exception as e:
                print(f"  [{done}/{len(to_run)}] {label} -- ERROR: {e}", flush=True)
                continue
            with cache_lock:
                cache[cache_key(p)] = {
                    "verdict": result.verdict.model_dump(),
                    "needs_recheck_reason": result.needs_recheck_reason,
                    "challenge": result.challenge.model_dump() if result.challenge else None,
                }
                _save_cache()
            results[id(p)] = (p, result)
            tag = "[repaired] " if id(p) in stuck_ids else "[sampled] "
            print(f"  {tag}[{done}/{len(to_run)}] {label} -- {result.verdict.verdict}"
                  + (f" (needs_recheck: {result.needs_recheck_reason})" if result.needs_recheck_reason else ""),
                  flush=True)

    print("\n--- Yield summary ---", flush=True)
    for pop_name, pop in (("repaired (was stuck)", stuck_p), ("sampled (never seen)", sampled_p)):
        verdicts = Counter()
        for p in pop:
            if id(p) not in results:
                continue
            _, result = results[id(p)]
            if result.needs_recheck_reason:
                verdicts["needs_recheck"] += 1
            else:
                verdicts[result.verdict.verdict] += 1
        print(f"  {pop_name} ({len(pop)} pairs): {dict(verdicts)}", flush=True)


if __name__ == "__main__":
    main()
