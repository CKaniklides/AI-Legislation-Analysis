# -*- coding: utf-8 -*-
"""
One-off (2026-09-24): rebuilds data/findings_c1.json from the CURRENT adjudication
cache and CURRENT code, with no new LLM calls. Needed after adding paragraph_index to
build_finding()'s provisions dict (alongside norm_index, per an external review) --
the cache itself didn't change, only what build_finding() writes out, so this just
re-runs the deterministic + cache-backed finding-construction logic (the same as
migrate_norm_identity_fix.py's tail section) without repeating that script's cache
migration (which must only ever run once against the pre-fix cache).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_c1_contradiction as c1
import networkx as nx

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def main():
    from openai import OpenAI
    client = OpenAI()
    model = c1.DEFAULT_MODEL

    print("Regenerating candidates (same defaults as detect_c1_contradiction.main())...", flush=True)
    records = c1.load_all_norm_records()
    g = nx.read_gexf(DATA / "graph.gexf")
    import similarity
    semantic_pairs = similarity.semantic_candidate_pairs(
        client, [r.text for r in records], k=8,
        groups=[r.instrument_id for r in records], k_within=3)
    exceptions = c1.load_conditional_exceptions()
    candidates = c1.generate_candidates(records, g, semantic_pairs)
    prepared = [p for p in (c1._prepare_candidate(c, exceptions) for c in candidates)
                if p["subtype"] is not None]
    AI_JUDGED = ("duty_conflict", "deontic_polarity_conflict")
    needs_llm = [p for p in prepared if p["subtype"] in AI_JUDGED]
    no_llm = [p for p in prepared if p["subtype"] not in AI_JUDGED]
    print(f"  {len(needs_llm)} AI-judged candidates, {len(no_llm)} deterministic", flush=True)

    cache = json.loads((DATA / "c1_adjudication_cache.json").read_text(encoding="utf-8"))

    findings, suppressed = [], []

    def finalize(p, llm_verdict=None, needs_recheck_reason=None, challenge=None):
        tier, tier_reasons = c1.compute_confidence_tier(p["a"], p["b"], p["subtype"], p["rk"],
                                                          p.get("det_result"), llm_verdict,
                                                          needs_recheck_reason, challenge)
        finding = c1.build_finding(p["a"], p["b"], p["c"], p["subtype"], p.get("det_result"),
                                    llm_verdict, tier, tier_reasons, needs_recheck_reason,
                                    p.get("unresolved_exception"), challenge)
        if p["suppressing_text"]:
            finding["status"] = "managed"
            finding["resolution_filter"] = {"checked": True, "deference_found": p["suppressing_text"],
                                             "result": "suppressed"}
            suppressed.append(finding)
        else:
            findings.append(finding)

    for p in no_llm:
        finalize(p)

    _VERDICT_FIELD_DEFAULTS = {"compliant_alternative": None, "outcome_changing_facts": None}
    n_from_cache = 0
    for p in needs_llm:
        key = f"{model}::{c1._pair_id(p['a'], p['b'])}"
        if key not in cache:
            continue
        entry = cache[key]
        challenge = c1.ChallengeVerdict(**entry["challenge"]) if entry.get("challenge") else None
        verdict = c1.DutyConflictVerdict(**{**_VERDICT_FIELD_DEFAULTS, **entry["verdict"]})
        needs_recheck_reason = entry["needs_recheck_reason"]
        n_from_cache += 1
        if needs_recheck_reason:
            finalize(p, verdict, needs_recheck_reason)
        elif verdict.verdict == "NOT_A_CONFLICT":
            continue
        else:
            finalize(p, verdict, None, challenge)

    out_path = DATA / "findings_c1.json"
    out_path.write_text(json.dumps({"findings": findings, "suppressed": suppressed},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  {n_from_cache} cache-backed pair(s) processed -> {len(findings)} finding(s), "
          f"{len(suppressed)} suppressed -> {out_path.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
