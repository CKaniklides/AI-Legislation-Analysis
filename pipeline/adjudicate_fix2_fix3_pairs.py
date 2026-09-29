# -*- coding: utf-8 -*-
"""
One-off (2026-09-28): bounded strict adjudication for exactly the pairs today's two fixes
created or re-routed -- NOT the pre-existing 4,498-pair unjudged backlog (that's a separate,
larger cost decision). Writes into the shared c1_adjudication_cache.json under the standard
key so rebuild_c1_all.py picks these up on its next (free, cache-only) replay.

  730 new needs_llm candidate pairs touching one of the 30 addressee_type-recovered norms
    9 former threshold_mismatch pairs, now routed to duty_conflict because the boilerplate
      stopword fix correctly revealed they never shared real violation-type evidence
  = 739 luna calls (reasoning off, temperature 0), same model/settings as every other strict
    C1 call in this project.
"""
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_c1_contradiction as c1
import networkx as nx
from dotenv import load_dotenv


def main():
    load_dotenv(c1.ROOT / ".env")
    from openai import OpenAI
    client = OpenAI()
    model = c1.DEFAULT_MODEL

    print("Loading norms, graph, embeddings...", flush=True)
    records = c1.load_all_norm_records()
    g = nx.read_gexf(c1.DATA / "graph.gexf")
    import similarity
    sp = similarity.semantic_candidate_pairs(client, [r.text for r in records], k=8,
                                             groups=[r.instrument_id for r in records], k_within=3)
    exceptions = c1.load_conditional_exceptions()
    candidates = c1.generate_candidates(records, g, sp)
    prepared = [p for p in (c1._prepare_candidate(c, exceptions) for c in candidates) if p["subtype"] is not None]
    AI_JUDGED = ("duty_conflict", "deontic_polarity_conflict")
    needs_llm = [p for p in prepared if p["subtype"] in AI_JUDGED]

    cache_path = c1.DATA / "c1_adjudication_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    uncached = [p for p in needs_llm if f"{model}::{c1._pair_id(p['a'], p['b'])}" not in cache]

    recovered_nids = set()
    for path, get in c1.SOURCES:
        root = json.loads((c1.ROOT / path).read_text(encoding="utf-8"))
        for p in get(root):
            inst = p.get("instrument_id") or ("BWBR0051796" if "uitvoeringswet" in path.lower() else "BWBR0049497")
            art = str(p.get("article") or p.get("number"))
            all_norms = p.get("norms", [])
            for norm in all_norms:
                if (norm["deontic"] in c1.ELIGIBLE_DEONTICS and norm.get("addressee_type") is None
                        and c1._infer_addressee_type(inst, art, norm, all_norms)):
                    recovered_nids.add(f"{inst}:{art}:{norm.get('paragraph_index')}:{norm.get('number')}:{norm.get('norm_index')}")

    old_findings = json.loads((c1.DATA / "findings_c1.json").read_text(encoding="utf-8"))["findings"]
    ft_nids = {f"{pr['instrument_id']}:{pr['article']}:{pr.get('paragraph_index')}:{pr.get('paragraph_number')}:{pr.get('norm_index')}"
               for f in old_findings if f["subtype"] == "threshold_mismatch" for pr in f["provisions"]}

    target = [p for p in uncached if c1._norm_id(p["a"]) in recovered_nids or c1._norm_id(p["b"]) in recovered_nids
              or (c1._norm_id(p["a"]) in ft_nids and c1._norm_id(p["b"]) in ft_nids)]
    print(f"target pairs: {len(target)} (of {len(uncached)} total uncached needs_llm pairs; "
          f"the remaining {len(uncached) - len(target)} are the pre-existing backlog, left untouched)", flush=True)

    paragraph_text_index = c1._build_paragraph_text_index(records)
    reverse_reference_index = c1._build_reverse_reference_index(records)
    lock = threading.Lock()
    n = 0

    def run(p):
        return p, c1.adjudicate_duty_conflict(client, model, p["a"], p["b"], paragraph_text_index, reverse_reference_index)

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(run, p) for p in target]
        for f in as_completed(futs):
            p, result = f.result()
            key = f"{model}::{c1._pair_id(p['a'], p['b'])}"
            with lock:
                cache[key] = {"verdict": result.verdict.model_dump(),
                              "needs_recheck_reason": result.needs_recheck_reason,
                              "challenge": result.challenge.model_dump() if result.challenge else None}
                cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
                n += 1
                if n % 50 == 0 or n == len(target):
                    print(f"  [{n}/{len(target)}]", flush=True)
            if result.verdict.verdict == "CONTRADICTION":
                print(f"  !! CONTRADICTION: {p['a'].instrument_id} art.{p['a'].article} <-> "
                      f"{p['b'].instrument_id} art.{p['b'].article}", flush=True)

    verdicts = {}
    for p in target:
        v = cache[f"{model}::{c1._pair_id(p['a'], p['b'])}"]["verdict"]["verdict"]
        verdicts[v] = verdicts.get(v, 0) + 1
    print(f"\ndone: {verdicts}", flush=True)


if __name__ == "__main__":
    main()
